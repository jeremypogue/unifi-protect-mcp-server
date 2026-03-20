#!/usr/bin/env python3
"""UniFi Protect MCP Server - Built from scratch for mcporter compatibility"""

import argparse
import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent, ImageContent
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route
from uiprotect import ProtectApiClient
from uiprotect.data.types import ModelType, EventType, SmartDetectObjectType
import uvicorn

# Directory for saving snapshots/videos
MEDIA_DIR = Path(os.getenv("UNIFI_PROTECT_MEDIA_DIR", "/tmp/unifi-protect-media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Initialize MCP server
app = Server("unifi-protect")

# Global Protect client
protect_client: ProtectApiClient | None = None


async def get_protect_client() -> ProtectApiClient:
    """Get or create Protect API client"""
    global protect_client
    
    if protect_client is None:
        host = os.getenv("UNIFI_PROTECT_HOST", "10.0.201.1")
        port = int(os.getenv("UNIFI_PROTECT_PORT", "443"))
        username = os.getenv("UNIFI_PROTECT_USERNAME", "vision")
        password = os.getenv("UNIFI_PROTECT_PASSWORD")
        verify_ssl = os.getenv("UNIFI_PROTECT_VERIFY_SSL", "false").lower() == "true"
        
        if not password:
            raise ValueError("UNIFI_PROTECT_PASSWORD environment variable required")
        
        protect_client = ProtectApiClient(
            host=host,
            port=port,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
        )
        
        await protect_client.update()
    
    return protect_client


async def find_camera(client, camera_id: str):
    """Find a camera by ID or name (case-insensitive)"""
    if camera_id in client.bootstrap.cameras:
        return client.bootstrap.cameras[camera_id]
    for cam in client.bootstrap.cameras.values():
        if cam.name and cam.name.lower() == camera_id.lower():
            return cam
    return None


async def find_any_device(client, device_id: str):
    """Find any adoptable device by ID, name, or MAC across all device types"""
    device_collections = {
        "camera": client.bootstrap.cameras,
        "light": client.bootstrap.lights,
        "sensor": client.bootstrap.sensors,
        "doorlock": client.bootstrap.doorlocks,
        "chime": client.bootstrap.chimes,
    }
    for dev_type, collection in device_collections.items():
        if device_id in collection:
            return collection[device_id], dev_type
        for dev in collection.values():
            if hasattr(dev, 'name') and dev.name and dev.name.lower() == device_id.lower():
                return dev, dev_type
            if hasattr(dev, 'mac') and dev.mac and dev.mac.lower().replace(':', '') == device_id.lower().replace(':', ''):
                return dev, dev_type
    return None, None


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available UniFi Protect tools"""
    return [
        Tool(
            name="list_cameras",
            description="List all UniFi Protect cameras with details",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_camera",
            description="Get detailed information about a specific camera",
            inputSchema={
                "type": "object",
                "properties": {"camera_id": {"type": "string", "description": "Camera ID or name"}},
                "required": ["camera_id"],
            },
        ),
        Tool(
            name="get_snapshot",
            description="Get a live snapshot image from a camera. Returns the image as base64-encoded JPEG.",
            inputSchema={
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "Camera ID or name"},
                    "width": {"type": "integer", "description": "Image width in pixels (optional, default 1920)"},
                    "height": {"type": "integer", "description": "Image height in pixels (optional, default 1080)"},
                },
                "required": ["camera_id"],
            },
        ),
        Tool(
            name="get_stream_url",
            description="Get RTSPS stream URLs for a camera to view live video. Returns available stream URLs by quality level.",
            inputSchema={
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "Camera ID or name"},
                },
                "required": ["camera_id"],
            },
        ),
        Tool(
            name="get_video_clip",
            description="Download a video clip from a camera's recording for a specific time range. Saves to file and returns the path. Max 5 minute clips.",
            inputSchema={
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "Camera ID or name"},
                    "minutes_ago": {"type": "integer", "description": "How many minutes ago to start the clip (default: 5)"},
                    "duration_seconds": {"type": "integer", "description": "Clip duration in seconds (default: 30, max: 300)"},
                },
                "required": ["camera_id"],
            },
        ),
        Tool(
            name="set_camera_recording",
            description="Enable or disable camera recording",
            inputSchema={
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["camera_id", "enabled"],
            },
        ),
        Tool(
            name="set_camera_led",
            description="Turn camera status LED on or off",
            inputSchema={
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["camera_id", "enabled"],
            },
        ),
        Tool(
            name="reboot_camera",
            description="Reboot a camera",
            inputSchema={
                "type": "object",
                "properties": {"camera_id": {"type": "string"}},
                "required": ["camera_id"],
            },
        ),
        Tool(
            name="get_system_info",
            description="Get UniFi Protect system information",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_adoptable_devices",
            description="List all devices visible to the NVR that can be adopted, including those adopted by other NVRs. Refreshes the bootstrap to discover new devices.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="adopt_device",
            description="Adopt a device into this UniFi Protect NVR. Use list_adoptable_devices first to find device IDs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "Device ID, name, or MAC address"},
                    "name": {"type": "string", "description": "Optional name to assign after adoption"},
                },
                "required": ["device_id"],
            },
        ),
        Tool(
            name="force_adopt_device",
            description="Force adopt a device, even if it is currently adopted by another NVR. This calls the adopt API directly, bypassing the can_adopt check. The device must be visible to the NVR.",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "Device ID, name, or MAC address"},
                    "device_type": {
                        "type": "string",
                        "description": "Device type (camera, light, sensor, doorlock, chime). Required if device is not yet in bootstrap.",
                        "enum": ["camera", "light", "sensor", "doorlock", "chime"],
                    },
                    "name": {"type": "string", "description": "Optional name to assign after adoption"},
                },
                "required": ["device_id"],
            },
        ),
        Tool(
            name="unadopt_device",
            description="Unadopt/unmanage a device from this UniFi Protect NVR. The device will be released and can be adopted by another NVR.",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "Device ID, name, or MAC address"},
                },
                "required": ["device_id"],
            },
        ),
        Tool(
            name="list_events",
            description="List events from UniFi Protect (motion, smart detections, camera connect/disconnect, etc.). "
                        "Filter by camera, event type, smart detect type, category, and time range. "
                        "Returns events sorted by most recent first by default.",
            inputSchema={
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "Camera ID or name to filter events for a specific camera (optional)"},
                    "event_type": {
                        "type": "string",
                        "description": "Event type filter (optional). Common values: motion, smartDetectZone, smartDetectLine, "
                                       "ring, disconnect, cameraConnected, cameraDisconnected, sensorMotion, sensorOpened, sensorClosed",
                    },
                    "smart_detect_type": {
                        "type": "string",
                        "description": "Smart detection type filter (optional). Values: person, animal, vehicle, licensePlate, "
                                       "package, face, car, pet, alrmSmoke, alrmBabyCry, alrmSpeak, alrmBark, alrmGlassBreak",
                    },
                    "category": {
                        "type": "string",
                        "description": "Event category filter (optional)",
                        "enum": ["critical", "update", "admin", "ring", "motion", "smart", "iot"],
                    },
                    "hours_back": {"type": "number", "description": "How many hours back to search (default: 24, max: 168)"},
                    "limit": {"type": "integer", "description": "Maximum number of events to return (default: 25, max: 100)"},
                },
            },
        ),
        Tool(
            name="get_event_thumbnail",
            description="Get the thumbnail image for a specific UniFi Protect event. Returns the image as base64-encoded JPEG. "
                        "Use list_events first to find event IDs with thumbnails.",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Event ID to get the thumbnail for"},
                    "width": {"type": "integer", "description": "Thumbnail width in pixels (optional)"},
                    "height": {"type": "integer", "description": "Thumbnail height in pixels (optional)"},
                },
                "required": ["event_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> Sequence[TextContent | ImageContent]:
    """Handle tool calls"""
    try:
        if not isinstance(arguments, dict):
            arguments = {}
        
        client = await get_protect_client()
        
        # Always refresh bootstrap data so we return live state
        await client.update()
        
        if name == "list_cameras":
            result_cameras = []
            
            for camera_id, camera in client.bootstrap.cameras.items():
                try:
                    cam_info = {
                        "id": camera_id,
                        "name": camera.name,
                        "model": camera.type,
                        "state": str(camera.state).replace("StateType.", ""),
                        "is_connected": camera.is_connected,
                        "is_recording": camera.is_recording,
                        "host": str(camera.host),
                        "mac": camera.mac,
                        "firmware_version": camera.firmware_version,
                    }
                    result_cameras.append(cam_info)
                except Exception as e:
                    result_cameras.append({"id": camera_id, "error": str(e)})
            
            result = {
                "total": len(result_cameras),
                "cameras": result_cameras
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_camera":
            camera_id = arguments.get("camera_id")
            if not camera_id:
                return [TextContent(type="text", text="Error: camera_id required")]
            
            camera = await find_camera(client, camera_id)
            if not camera:
                return [TextContent(type="text", text=f"Error: Camera '{camera_id}' not found")]
            
            result = {
                "id": camera.id,
                "name": camera.name,
                "model": camera.type,
                "state": str(camera.state),
                "is_connected": camera.is_connected,
                "is_recording": camera.is_recording,
                "is_dark": camera.is_dark,
                "host": str(camera.host),
                "mac": camera.mac,
                "firmware_version": camera.firmware_version,
                "last_motion": str(camera.last_motion) if camera.last_motion else None,
                "up_since": str(camera.up_since) if camera.up_since else None,
            }
            
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_snapshot":
            camera_id = arguments.get("camera_id")
            if not camera_id:
                return [TextContent(type="text", text="Error: camera_id required")]
            
            camera = await find_camera(client, camera_id)
            if not camera:
                return [TextContent(type="text", text=f"Error: Camera '{camera_id}' not found")]
            
            width = arguments.get("width", 1920)
            height = arguments.get("height", 1080)
            
            snapshot_bytes = await client.get_camera_snapshot(camera.id, width=width, height=height)
            if not snapshot_bytes:
                return [TextContent(type="text", text=f"Error: Failed to get snapshot from camera '{camera.name}'")]
            
            # Save to file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = camera.name.replace(" ", "_").lower()
            filename = f"snapshot_{safe_name}_{timestamp}.jpg"
            filepath = MEDIA_DIR / filename
            filepath.write_bytes(snapshot_bytes)
            
            # Return as base64 image + text with file path
            b64_data = base64.b64encode(snapshot_bytes).decode("utf-8")
            return [
                ImageContent(type="image", data=b64_data, mimeType="image/jpeg"),
                TextContent(type="text", text=json.dumps({
                    "camera": camera.name,
                    "resolution": f"{width}x{height}",
                    "file": str(filepath),
                    "size_bytes": len(snapshot_bytes),
                }, indent=2)),
            ]
        
        elif name == "get_stream_url":
            camera_id = arguments.get("camera_id")
            if not camera_id:
                return [TextContent(type="text", text="Error: camera_id required")]
            
            camera = await find_camera(client, camera_id)
            if not camera:
                return [TextContent(type="text", text=f"Error: Camera '{camera_id}' not found")]
            
            # Get existing RTSPS streams
            streams = await client.get_camera_rtsps_streams(camera.id)
            
            result = {
                "camera": camera.name,
                "camera_id": camera.id,
                "host": str(camera.host),
            }
            
            if streams:
                result["rtsps_streams"] = {}
                # streams is an RTSPSStreams object with quality-level attributes
                for quality in ["high", "medium", "low"]:
                    url = getattr(streams, quality, None)
                    if url:
                        result["rtsps_streams"][quality] = url
            
            # Also provide direct RTSP URL construction
            host = os.getenv("UNIFI_PROTECT_HOST", "10.0.201.1")
            port = int(os.getenv("UNIFI_PROTECT_PORT", "443"))
            result["note"] = f"RTSPS streams require the UniFi Protect NVR at {host}:{port}. Use VLC or ffmpeg to view."
            
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_video_clip":
            camera_id = arguments.get("camera_id")
            if not camera_id:
                return [TextContent(type="text", text="Error: camera_id required")]
            
            camera = await find_camera(client, camera_id)
            if not camera:
                return [TextContent(type="text", text=f"Error: Camera '{camera_id}' not found")]
            
            minutes_ago = min(arguments.get("minutes_ago", 5), 60)
            duration_seconds = min(arguments.get("duration_seconds", 30), 300)
            
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(minutes=minutes_ago)
            clip_end = start_time + timedelta(seconds=duration_seconds)
            
            # Don't let clip_end exceed now
            if clip_end > end_time:
                clip_end = end_time
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = camera.name.replace(" ", "_").lower()
            filename = f"clip_{safe_name}_{timestamp}.mp4"
            filepath = MEDIA_DIR / filename
            
            video_bytes = await client.get_camera_video(
                camera.id,
                start=start_time,
                end=clip_end,
                output_file=filepath,
            )
            
            if not filepath.exists() or filepath.stat().st_size == 0:
                return [TextContent(type="text", text=f"Error: Failed to download video clip from camera '{camera.name}'. The camera may not have recordings for the requested time range.")]
            
            file_size = filepath.stat().st_size
            result = {
                "camera": camera.name,
                "file": str(filepath),
                "size_bytes": file_size,
                "size_mb": round(file_size / 1048576, 2),
                "start": start_time.isoformat(),
                "end": clip_end.isoformat(),
                "duration_seconds": duration_seconds,
            }
            
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "set_camera_recording":
            camera_id = arguments.get("camera_id")
            enabled = arguments.get("enabled", True)
            
            camera = await find_camera(client, camera_id)
            if not camera:
                return [TextContent(type="text", text=f"Error: Camera '{camera_id}' not found")]
            
            camera.recording_settings.mode = "always" if enabled else "never"
            await client.update_device(camera)
            
            return [TextContent(type="text", text=f"Camera '{camera.name}' recording {'enabled' if enabled else 'disabled'}")]
        
        elif name == "set_camera_led":
            camera_id = arguments.get("camera_id")
            enabled = arguments.get("enabled", True)
            
            camera = await find_camera(client, camera_id)
            if not camera:
                return [TextContent(type="text", text=f"Error: Camera '{camera_id}' not found")]
            
            if hasattr(camera, 'led_settings'):
                camera.led_settings.is_enabled = enabled
                await client.update_device(camera)
                return [TextContent(type="text", text=f"Camera '{camera.name}' LED {'enabled' if enabled else 'disabled'}")]
            else:
                return [TextContent(type="text", text=f"Error: Camera '{camera.name}' does not support LED control")]
        
        elif name == "reboot_camera":
            camera_id = arguments.get("camera_id")
            
            camera = await find_camera(client, camera_id)
            if not camera:
                return [TextContent(type="text", text=f"Error: Camera '{camera_id}' not found")]
            
            await camera.reboot()
            return [TextContent(type="text", text=f"Camera '{camera.name}' rebooting...")]
        
        elif name == "get_system_info":
            nvr = client.bootstrap.nvr
            camera_count = len(client.bootstrap.cameras)
            
            result = {
                "name": nvr.name if nvr else "Unknown",
                "version": nvr.version if nvr else "Unknown",
                "firmware_version": nvr.firmware_version if nvr else "Unknown",
                "host": nvr.host if nvr else "Unknown",
                "total_cameras": camera_count,
            }
            
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "list_adoptable_devices":
            adoptable = []
            device_collections = {
                "camera": (client.bootstrap.cameras, ModelType.CAMERA),
                "light": (client.bootstrap.lights, ModelType.LIGHT),
                "sensor": (client.bootstrap.sensors, ModelType.SENSOR),
                "doorlock": (client.bootstrap.doorlocks, ModelType.DOORLOCK),
                "chime": (client.bootstrap.chimes, ModelType.CHIME),
            }
            
            for dev_type, (collection, model_type) in device_collections.items():
                for dev_id, dev in collection.items():
                    dev_info = {
                        "id": dev_id,
                        "type": dev_type,
                        "name": getattr(dev, 'name', None) or getattr(dev, 'market_name', None) or "Unknown",
                        "model": getattr(dev, 'type', 'unknown'),
                        "mac": getattr(dev, 'mac', None),
                        "host": str(getattr(dev, 'host', 'unknown')),
                        "state": str(getattr(dev, 'state', 'unknown')).replace("StateType.", ""),
                        "is_connected": getattr(dev, 'is_connected', False),
                        "is_adopted": getattr(dev, 'is_adopted', False),
                        "is_adopted_by_other": getattr(dev, 'is_adopted_by_other', False),
                        "can_adopt": getattr(dev, 'can_adopt', False),
                        "is_adopting": getattr(dev, 'is_adopting', False),
                        "firmware": getattr(dev, 'firmware_version', None),
                    }
                    adoptable.append(dev_info)
            
            # Separate into categories
            available = [d for d in adoptable if d["can_adopt"]]
            adopted_by_us = [d for d in adoptable if d["is_adopted"] and not d["is_adopted_by_other"]]
            adopted_by_other = [d for d in adoptable if d["is_adopted_by_other"]]
            adopting = [d for d in adoptable if d["is_adopting"]]
            
            result = {
                "total_devices": len(adoptable),
                "available_to_adopt": available,
                "adopted_by_this_nvr": adopted_by_us,
                "adopted_by_other_nvr": adopted_by_other,
                "currently_adopting": adopting,
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "adopt_device":
            device_id = arguments.get("device_id")
            device_name = arguments.get("name")
            if not device_id:
                return [TextContent(type="text", text="Error: device_id required")]
            
            device, dev_type = await find_any_device(client, device_id)
            if not device:
                return [TextContent(type="text", text=f"Error: Device '{device_id}' not found. Use list_adoptable_devices to see available devices.")]
            
            if not getattr(device, 'can_adopt', False):
                reasons = []
                if getattr(device, 'is_adopted', False) and not getattr(device, 'is_adopted_by_other', False):
                    reasons.append("already adopted by this NVR")
                if getattr(device, 'is_adopted_by_other', False):
                    reasons.append("adopted by another NVR (use force_adopt_device to override)")
                if getattr(device, 'is_adopting', False):
                    reasons.append("currently being adopted")
                reason_str = ", ".join(reasons) if reasons else "device reports can_adopt=false"
                return [TextContent(type="text", text=f"Error: Device '{getattr(device, 'name', device_id)}' cannot be adopted: {reason_str}")]
            
            try:
                await device.adopt(name=device_name)
                return [TextContent(type="text", text=f"Device '{getattr(device, 'name', device_id)}' ({dev_type}) adoption initiated successfully. It may take a minute to fully connect.")]
            except Exception as e:
                return [TextContent(type="text", text=f"Error adopting device: {str(e)}")]
        
        elif name == "force_adopt_device":
            device_id = arguments.get("device_id")
            device_type = arguments.get("device_type")
            device_name = arguments.get("name")
            if not device_id:
                return [TextContent(type="text", text="Error: device_id required")]
            
            device, dev_type = await find_any_device(client, device_id)
            
            if device:
                actual_id = device.id
                # Map dev_type string to ModelType
                type_map = {
                    "camera": ModelType.CAMERA,
                    "light": ModelType.LIGHT,
                    "sensor": ModelType.SENSOR,
                    "doorlock": ModelType.DOORLOCK,
                    "chime": ModelType.CHIME,
                }
                model_type = type_map.get(dev_type, ModelType.CAMERA)
                dev_display = getattr(device, 'name', actual_id) or actual_id
            else:
                if not device_type:
                    return [TextContent(type="text", text="Error: Device not found in bootstrap. Provide device_type (camera, light, sensor, doorlock, chime) along with the device ID to force adopt.")]
                actual_id = device_id
                type_map = {
                    "camera": ModelType.CAMERA,
                    "light": ModelType.LIGHT,
                    "sensor": ModelType.SENSOR,
                    "doorlock": ModelType.DOORLOCK,
                    "chime": ModelType.CHIME,
                }
                model_type = type_map.get(device_type, ModelType.CAMERA)
                dev_display = device_id
            
            try:
                # Call adopt_device directly on the API client, bypassing can_adopt check
                await client.adopt_device(model_type, actual_id)
                
                msg = f"Force adopt initiated for '{dev_display}' (type: {model_type.value}). The device should appear in the NVR within 1-2 minutes."
                if device and getattr(device, 'is_adopted_by_other', False):
                    msg += " Note: This device was previously adopted by another NVR."
                
                if device_name:
                    try:
                        # Wait briefly and try to set name
                        await asyncio.sleep(3)
                        await client.update()
                        device2, _ = await find_any_device(client, actual_id)
                        if device2:
                            await device2.set_name(device_name)
                            msg += f" Name set to '{device_name}'."
                    except Exception:
                        msg += f" Could not set name yet — try renaming after adoption completes."
                
                return [TextContent(type="text", text=msg)]
            except Exception as e:
                return [TextContent(type="text", text=f"Error force adopting device: {str(e)}")]
        
        elif name == "unadopt_device":
            device_id = arguments.get("device_id")
            if not device_id:
                return [TextContent(type="text", text="Error: device_id required")]
            
            device, dev_type = await find_any_device(client, device_id)
            if not device:
                return [TextContent(type="text", text=f"Error: Device '{device_id}' not found")]
            
            if not getattr(device, 'is_adopted', False) or getattr(device, 'is_adopted_by_other', False):
                return [TextContent(type="text", text=f"Error: Device '{getattr(device, 'name', device_id)}' is not adopted by this NVR")]
            
            dev_display = getattr(device, 'name', device_id) or device_id
            try:
                await device.unadopt()
                return [TextContent(type="text", text=f"Device '{dev_display}' ({dev_type}) has been unadopted/unmanaged from this NVR.")]
            except Exception as e:
                return [TextContent(type="text", text=f"Error unadopting device: {str(e)}")]
        
        elif name == "list_events":
            # Time range
            hours_back = min(arguments.get("hours_back", 24), 168)
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=hours_back)
            limit = min(arguments.get("limit", 25), 100)
            
            # Build filter kwargs
            get_events_kwargs: dict[str, Any] = {
                "start": start_time,
                "end": end_time,
                "limit": limit,
                "sorting": "desc",
                "descriptions": True,
            }
            
            # Event type filter
            event_type_str = arguments.get("event_type")
            if event_type_str:
                # Match by EventType value
                matched_type = None
                for et in EventType:
                    if et.value == event_type_str or et.value.lower() == event_type_str.lower():
                        matched_type = et
                        break
                if matched_type:
                    get_events_kwargs["types"] = [matched_type]
                else:
                    return [TextContent(type="text", text=f"Error: Unknown event_type '{event_type_str}'. Use values like: motion, smartDetectZone, ring, disconnect, cameraConnected, cameraDisconnected")]
            
            # Smart detect type filter
            smart_type_str = arguments.get("smart_detect_type")
            if smart_type_str:
                matched_smart = None
                for st in SmartDetectObjectType:
                    if st.value == smart_type_str or st.value.lower() == smart_type_str.lower():
                        matched_smart = st
                        break
                if matched_smart:
                    get_events_kwargs["smart_detect_types"] = [matched_smart]
                else:
                    return [TextContent(type="text", text=f"Error: Unknown smart_detect_type '{smart_type_str}'. Use values like: person, animal, vehicle, licensePlate, package, face")]
            
            # Category filter
            category_str = arguments.get("category")
            if category_str:
                get_events_kwargs["category"] = category_str
            
            # Fetch events
            events = await client.get_events(**get_events_kwargs)
            
            # Camera filter (post-query since get_events doesn't have a camera param)
            camera_id_filter = arguments.get("camera_id")
            target_camera_id = None
            if camera_id_filter:
                cam = await find_camera(client, camera_id_filter)
                if not cam:
                    return [TextContent(type="text", text=f"Error: Camera '{camera_id_filter}' not found")]
                target_camera_id = cam.id
            
            # Build camera name lookup
            cam_names = {cid: c.name for cid, c in client.bootstrap.cameras.items()}
            
            result_events = []
            for event in events:
                # Filter by camera if specified
                if target_camera_id and event.camera_id != target_camera_id:
                    continue
                
                evt = {
                    "id": event.id,
                    "type": event.type.value if hasattr(event.type, 'value') else str(event.type),
                    "start": event.start.isoformat() if event.start else None,
                    "end": event.end.isoformat() if event.end else None,
                    "score": event.score,
                    "camera_id": event.camera_id,
                    "camera_name": cam_names.get(event.camera_id, None),
                }
                
                if event.smart_detect_types:
                    evt["smart_detect_types"] = [s.value for s in event.smart_detect_types]
                
                if event.category:
                    evt["category"] = event.category
                
                if event.thumbnail_id:
                    evt["has_thumbnail"] = True
                    evt["thumbnail_id"] = event.thumbnail_id
                
                if event.heatmap_id:
                    evt["has_heatmap"] = True
                
                result_events.append(evt)
            
            result = {
                "total_returned": len(result_events),
                "time_range": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat(),
                    "hours_back": hours_back,
                },
                "events": result_events,
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_event_thumbnail":
            event_id = arguments.get("event_id")
            if not event_id:
                return [TextContent(type="text", text="Error: event_id required")]
            
            width = arguments.get("width")
            height = arguments.get("height")
            
            # First get the event to find the thumbnail_id
            try:
                event = await client.get_event(event_id)
            except Exception as e:
                return [TextContent(type="text", text=f"Error: Could not find event '{event_id}': {str(e)}")]
            
            if not event.thumbnail_id:
                return [TextContent(type="text", text=f"Error: Event '{event_id}' has no thumbnail")]
            
            # Fetch the thumbnail
            thumb_bytes = await client.get_event_thumbnail(
                event.thumbnail_id,
                width=width,
                height=height,
            )
            
            if not thumb_bytes:
                return [TextContent(type="text", text=f"Error: Failed to retrieve thumbnail for event '{event_id}'")]
            
            # Save to file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"event_thumb_{event_id[:12]}_{timestamp}.jpg"
            filepath = MEDIA_DIR / filename
            filepath.write_bytes(thumb_bytes)
            
            # Build camera name
            cam_names = {cid: c.name for cid, c in client.bootstrap.cameras.items()}
            
            b64_data = base64.b64encode(thumb_bytes).decode("utf-8")
            meta = {
                "event_id": event.id,
                "event_type": event.type.value if hasattr(event.type, 'value') else str(event.type),
                "camera_name": cam_names.get(event.camera_id, None),
                "start": event.start.isoformat() if event.start else None,
                "score": event.score,
                "file": str(filepath),
                "size_bytes": len(thumb_bytes),
            }
            if event.smart_detect_types:
                meta["smart_detect_types"] = [s.value for s in event.smart_detect_types]
            
            return [
                ImageContent(type="image", data=b64_data, mimeType="image/jpeg"),
                TextContent(type="text", text=json.dumps(meta, indent=2)),
            ]
        
        else:
            return [TextContent(type="text", text=f"Error: Unknown tool '{name}'")]
    
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return [TextContent(type="text", text=f"Error: {str(e)}\n\nTraceback:\n{tb}")]


async def main():
    """Run the MCP server"""
    parser = argparse.ArgumentParser(description="UniFi Protect MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=os.getenv("MCP_TRANSPORT", "sse"),
        help="Transport type (default: sse, or set MCP_TRANSPORT env var)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", "0.0.0.0"),
        help="Host to bind SSE server (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MCP_PORT", "8001")),
        help="Port for SSE server (default: 8001)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    else:
        # SSE transport
        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await app.run(
                    streams[0], streams[1], app.create_initialization_options()
                )
            return Response()

        starlette_app = Starlette(
            debug=False,
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ],
        )

        logger = logging.getLogger("unifi-protect-mcp")
        logging.basicConfig(level=logging.INFO)

        ssl_enabled = os.getenv("MCP_SSL_ENABLED", "false").lower() == "true"
        uvi_kwargs: dict[str, Any] = {
            "host": args.host,
            "port": args.port,
            "log_level": "info",
        }
        if ssl_enabled:
            uvi_kwargs["ssl_certfile"] = os.getenv("MCP_SSL_CERTFILE", "/home/vision/.ssl/mcp.crt")
            uvi_kwargs["ssl_keyfile"] = os.getenv("MCP_SSL_KEYFILE", "/home/vision/.ssl/mcp.key")

        scheme = "https" if ssl_enabled else "http"
        logger.info(f"Starting UniFi Protect MCP SSE server on {args.host}:{args.port}")
        logger.info(f"SSE endpoint: {scheme}://{args.host}:{args.port}/sse")

        config = uvicorn.Config(starlette_app, **uvi_kwargs)
        server = uvicorn.Server(config)
        await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
