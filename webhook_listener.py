#!/usr/bin/env python3
"""
UniFi Protect Webhook Listener — AI-powered event analysis

Receives webhook alerts from UniFi Protect on motion/smart detection events,
grabs a snapshot from the camera, and sends it to a vision-capable LLM via
the ollama-mcp-bridge for analysis. The LLM can describe what it sees and
optionally take actions through MCP tools (HA notifications, lights, etc.).

Usage:
    python webhook_listener.py [--port 8002] [--bridge-url http://localhost:8000]

UniFi Protect webhook URL: http://<this-host>:8002/webhook/protect
"""

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
from uiprotect import ProtectApiClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:8000")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl:8b")
PROTECT_HOST = os.getenv("UNIFI_PROTECT_HOST", "10.0.201.1")
PROTECT_PORT = int(os.getenv("UNIFI_PROTECT_PORT", "443"))
PROTECT_USERNAME = os.getenv("UNIFI_PROTECT_USERNAME", "vision")
PROTECT_PASSWORD = os.getenv("UNIFI_PROTECT_PASSWORD", "")
PROTECT_VERIFY_SSL = os.getenv("UNIFI_PROTECT_VERIFY_SSL", "false").lower() == "true"
LISTENER_PORT = int(os.getenv("WEBHOOK_PORT", "8002"))

# Rate limiting: minimum seconds between analyses per camera
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "30"))

# Snapshot settings
SNAPSHOT_WIDTH = int(os.getenv("SNAPSHOT_WIDTH", "1280"))
SNAPSHOT_HEIGHT = int(os.getenv("SNAPSHOT_HEIGHT", "720"))

# Media directory for saving snapshots
MEDIA_DIR = Path(os.getenv("UNIFI_PROTECT_MEDIA_DIR", "/tmp/unifi-protect-media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# System prompt for the vision model
ANALYSIS_SYSTEM_PROMPT = os.getenv("ANALYSIS_SYSTEM_PROMPT", """You are a security camera AI analyst for a home automation system. 
When shown a camera snapshot triggered by a motion/detection event, you should:
1. Describe what you see in the image concisely (1-3 sentences)
2. Assess the threat level: routine, attention, or alert
3. If the event involves a person, describe them briefly
4. If vehicles are visible, note them
5. Note anything unusual or out of place

Keep responses brief and factual. Format:
**Camera:** <name>
**Event:** <type>
**Description:** <what you see>
**Assessment:** <routine|attention|alert>
**Action:** <recommended action or "none needed">""")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("protect-webhook")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
protect_client: ProtectApiClient | None = None
camera_cooldowns: dict[str, float] = {}  # camera_id -> last analysis timestamp

# ---------------------------------------------------------------------------
# Protect API client
# ---------------------------------------------------------------------------
async def get_protect_client() -> ProtectApiClient:
    global protect_client
    if protect_client is None:
        if not PROTECT_PASSWORD:
            raise ValueError("UNIFI_PROTECT_PASSWORD environment variable required")
        protect_client = ProtectApiClient(
            host=PROTECT_HOST,
            port=PROTECT_PORT,
            username=PROTECT_USERNAME,
            password=PROTECT_PASSWORD,
            verify_ssl=PROTECT_VERIFY_SSL,
        )
        await protect_client.update()
        log.info(f"Connected to UniFi Protect at {PROTECT_HOST}:{PROTECT_PORT}")
        log.info(f"Found {len(protect_client.bootstrap.cameras)} cameras")
    return protect_client


async def find_camera(client, camera_id: str):
    """Find camera by ID or name"""
    if camera_id in client.bootstrap.cameras:
        return client.bootstrap.cameras[camera_id]
    for cam in client.bootstrap.cameras.values():
        if cam.name and cam.name.lower() == camera_id.lower():
            return cam
    return None


# ---------------------------------------------------------------------------
# Snapshot + LLM analysis
# ---------------------------------------------------------------------------
async def grab_snapshot(client, camera_id: str) -> tuple[bytes | None, str | None]:
    """Grab a snapshot from a camera, return (bytes, filepath)"""
    try:
        snapshot = await client.get_camera_snapshot(
            camera_id, width=SNAPSHOT_WIDTH, height=SNAPSHOT_HEIGHT
        )
        if snapshot:
            camera = client.bootstrap.cameras.get(camera_id)
            cam_name = camera.name if camera else camera_id
            safe_name = cam_name.replace(" ", "_").lower()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = MEDIA_DIR / f"event_{safe_name}_{timestamp}.jpg"
            filepath.write_bytes(snapshot)
            return snapshot, str(filepath)
    except Exception as e:
        log.error(f"Failed to grab snapshot from {camera_id}: {e}")
    return None, None


async def analyze_with_llm(
    camera_name: str,
    event_type: str,
    smart_types: list[str],
    snapshot_b64: str,
    score: int,
) -> str | None:
    """Send snapshot to vision LLM via the bridge for analysis"""
    event_desc = event_type
    if smart_types:
        event_desc += f" ({', '.join(smart_types)})"

    user_message = (
        f"Motion/detection alert from camera '{camera_name}'. "
        f"Event type: {event_desc}. Detection confidence: {score}%. "
        f"Analyze this camera snapshot and describe what you see."
    )

    payload = {
        "model": VISION_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": user_message,
                "images": [snapshot_b64],
            },
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{BRIDGE_URL}/api/chat", json=payload)
            resp.raise_for_status()
            result = resp.json()
            content = result.get("message", {}).get("content", "")
            return content
    except Exception as e:
        log.error(f"LLM analysis failed: {e}")
        return None


async def send_ha_notification(analysis: str, camera_name: str, filepath: str | None):
    """Optionally send the analysis as a Home Assistant notification via the bridge"""
    # Only send for non-routine assessments
    if "routine" in analysis.lower() and "alert" not in analysis.lower():
        return

    notification_text = f"🎥 Camera Alert: {camera_name}\n\n{analysis}"
    if filepath:
        notification_text += f"\n\nSnapshot: {filepath}"

    payload = {
        "model": VISION_MODEL,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Send a Home Assistant notification with this security alert. "
                    f"Use the ha_notify tool with title 'Camera Alert: {camera_name}' "
                    f"and this message:\n\n{analysis}"
                ),
            },
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{BRIDGE_URL}/api/chat", json=payload)
            if resp.status_code == 200:
                log.info(f"HA notification sent for {camera_name}")
    except Exception as e:
        log.warning(f"Failed to send HA notification: {e}")


# ---------------------------------------------------------------------------
# Webhook event processing
# ---------------------------------------------------------------------------
async def process_event(event_data: dict[str, Any]):
    """Process a single UniFi Protect event"""
    camera_id = event_data.get("camera") or event_data.get("camera_id") or event_data.get("cameraId")
    if not camera_id:
        log.warning("Webhook event missing camera ID, skipping")
        return

    # Rate limiting per camera
    now = time.time()
    last_time = camera_cooldowns.get(camera_id, 0)
    if now - last_time < COOLDOWN_SECONDS:
        log.debug(f"Cooldown active for camera {camera_id}, skipping ({COOLDOWN_SECONDS}s)")
        return
    camera_cooldowns[camera_id] = now

    # Extract event info
    event_type = event_data.get("type", event_data.get("eventType", "motion"))
    smart_types = event_data.get("smartDetectTypes", event_data.get("smart_detect_types", []))
    score = event_data.get("score", event_data.get("confidence", 0))

    # Normalize smart_types
    if isinstance(smart_types, str):
        smart_types = [smart_types]

    try:
        client = await get_protect_client()
        camera = await find_camera(client, camera_id)
        camera_name = camera.name if camera else camera_id

        log.info(f"🎥 Event: {event_type} on '{camera_name}' (smart: {smart_types}, score: {score})")

        # Grab snapshot
        snapshot_bytes, filepath = await grab_snapshot(client, camera_id)
        if not snapshot_bytes:
            log.warning(f"No snapshot available for {camera_name}")
            return

        snapshot_b64 = base64.b64encode(snapshot_bytes).decode("utf-8")
        log.info(f"📸 Snapshot captured: {filepath} ({len(snapshot_bytes)} bytes)")

        # Analyze with vision LLM
        analysis = await analyze_with_llm(camera_name, event_type, smart_types, snapshot_b64, score)
        if analysis:
            log.info(f"🤖 Analysis for '{camera_name}':\n{analysis}")

            # Send HA notification for non-routine events
            await send_ha_notification(analysis, camera_name, filepath)
        else:
            log.warning(f"No analysis returned for {camera_name}")

    except Exception as e:
        log.error(f"Error processing event for camera {camera_id}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="UniFi Protect Webhook Listener",
    description="Receives UniFi Protect alerts and triggers AI-powered visual analysis",
    version="1.0.0",
)


@app.get("/health")
async def health():
    """Health check"""
    return {
        "status": "healthy",
        "bridge_url": BRIDGE_URL,
        "vision_model": VISION_MODEL,
        "protect_host": PROTECT_HOST,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "cameras_tracked": len(camera_cooldowns),
    }


@app.post("/webhook/protect")
async def receive_webhook(request: Request):
    """
    Receive UniFi Protect webhook alerts.
    
    UniFi Protect can send webhooks in various formats. This endpoint
    handles the common payload structures.
    """
    try:
        body = await request.json()
    except Exception:
        # Some webhooks may send form data or text
        body_bytes = await request.body()
        try:
            body = json.loads(body_bytes)
        except Exception:
            log.warning(f"Could not parse webhook body: {body_bytes[:200]}")
            return JSONResponse(
                status_code=400,
                content={"error": "Could not parse request body"},
            )

    log.info(f"📨 Webhook received: {json.dumps(body, default=str)[:500]}")

    # Handle different payload formats
    # Format 1: Direct event object
    if "camera" in body or "camera_id" in body or "cameraId" in body:
        asyncio.create_task(process_event(body))
        return {"status": "accepted", "message": "Event queued for analysis"}

    # Format 2: Wrapped in "data" or "event" key
    event = body.get("data") or body.get("event") or body.get("payload")
    if event and isinstance(event, dict):
        asyncio.create_task(process_event(event))
        return {"status": "accepted", "message": "Event queued for analysis"}

    # Format 3: Array of events
    events = body.get("events") or body.get("data")
    if events and isinstance(events, list):
        for evt in events:
            if isinstance(evt, dict):
                asyncio.create_task(process_event(evt))
        return {"status": "accepted", "message": f"{len(events)} events queued"}

    # Unknown format — log and accept anyway
    log.warning(f"Unknown webhook payload format, attempting to process as-is")
    asyncio.create_task(process_event(body))
    return {"status": "accepted", "message": "Event queued (unknown format)"}


@app.post("/webhook/test")
async def test_webhook(request: Request):
    """
    Test endpoint: simulate a motion event on a camera.
    
    POST with: {"camera_id": "<name or id>", "type": "motion"}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "JSON body required"})

    camera_id = body.get("camera_id", body.get("camera"))
    if not camera_id:
        # Default to first camera
        try:
            client = await get_protect_client()
            if client.bootstrap.cameras:
                first_cam = next(iter(client.bootstrap.cameras.values()))
                camera_id = first_cam.id
                log.info(f"No camera specified, using first camera: {first_cam.name}")
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"Cannot connect to Protect: {e}"})

    if not camera_id:
        return JSONResponse(status_code=400, content={"error": "camera_id required"})

    # Reset cooldown for test
    camera_cooldowns.pop(camera_id, None)

    event = {
        "camera": camera_id,
        "type": body.get("type", "motion"),
        "smartDetectTypes": body.get("smartDetectTypes", ["person"]),
        "score": body.get("score", 85),
    }

    asyncio.create_task(process_event(event))
    return {"status": "accepted", "message": f"Test event queued for camera {camera_id}"}


@app.get("/cameras")
async def list_cameras():
    """List available cameras for reference"""
    try:
        client = await get_protect_client()
        cameras = []
        for cam_id, cam in client.bootstrap.cameras.items():
            cameras.append({
                "id": cam_id,
                "name": cam.name,
                "is_connected": cam.is_connected,
            })
        return {"cameras": cameras}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    log.info(f"Starting UniFi Protect Webhook Listener on port {LISTENER_PORT}")
    log.info(f"Webhook URL: http://0.0.0.0:{LISTENER_PORT}/webhook/protect")
    log.info(f"Bridge URL: {BRIDGE_URL}")
    log.info(f"Vision model: {VISION_MODEL}")
    log.info(f"Cooldown: {COOLDOWN_SECONDS}s per camera")
    uvicorn.run(app, host="0.0.0.0", port=LISTENER_PORT)


if __name__ == "__main__":
    main()
