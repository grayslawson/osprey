"""What a camera answers when the controller configures it.

The controller sends its settings suite one message at a time and waits for each
to be acknowledged. Most answers are an echo of what was asked for: the camera
reports its resulting state, and the controller stores that. Get the shape wrong
and the controller either stalls waiting or records nothing useful.

The payload shapes come from three places, in this order of authority: our own
capture of the real controller talking to the real camera, the real camera's
record in the controller, and — for the fields we never saw a camera *send* —
unifi-cam-proxy, which is a working implementation against real Protect.
"""

from __future__ import annotations

from typing import Any, Final

import identity

ISP_ECHO_KEYS: Final = (
    "aeMode", "aeTargetPercent", "aggressiveAntiFlicker", "brightness", "contrast",
    "denoise", "digitalZoom", "dZoomStreamId", "enable3dnr", "enableExternalIr",
    "enableMicroFocus", "enablePauseMotion", "flip", "focusMode", "focusPosition",
    "hue", "icrLightSensorNightThd", "icrSensitivity", "irLedLevel", "irLedMode",
    "irOnStsBrightness", "irOnStsContrast", "irOnStsDenoise", "irOnStsHue",
    "irOnStsSaturation", "irOnStsSharpness", "irOnValBrightness", "irOnValContrast",
    "irOnValDenoise", "irOnValHue", "irOnValSaturation", "irOnValSharpness",
    "lensDistortionCorrection", "mirror", "saturation", "sharpness",
    "touchFocusX", "touchFocusY", "wdr", "zoomPosition",
)


def isp_settings(asked: dict[str, Any]) -> dict[str, Any]:
    """Echo the ISP settings back with sensible values for anything not asked.

    The controller treats the reply as the camera's actual state, so echoing what
    it just set is both honest and what a real camera does.
    """
    reply: dict[str, Any] = {
        "aeMode": "auto",
        "aeTargetPercent": 50,
        "aggressiveAntiFlicker": 0,
        "brightness": 50,
        "contrast": 50,
        "denoise": 50,
        "digitalZoom": 0,
        "dZoomStreamId": 4,
        "enable3dnr": 1,
        "enableExternalIr": 0,
        "enableMicroFocus": 0,
        "enablePauseMotion": 0,
        "flip": 0,
        "focusMode": "ztrig",
        "focusPosition": 0,
        "hue": 50,
        "icrLightSensorNightThd": 0,
        "icrSensitivity": 0,
        "irLedLevel": 255,
        "irLedMode": "auto",
        "irOnStsBrightness": 0,
        "irOnStsContrast": 0,
        "irOnStsDenoise": 0,
        "irOnStsHue": 0,
        "irOnStsSaturation": 0,
        "irOnStsSharpness": 0,
        "irOnValBrightness": 50,
        "irOnValContrast": 50,
        "irOnValDenoise": 50,
        "irOnValHue": 50,
        "irOnValSaturation": 50,
        "irOnValSharpness": 50,
        "lensDistortionCorrection": 1,
        "mirror": 0,
        "saturation": 50,
        "sharpness": 50,
        "touchFocusX": 1001,
        "touchFocusY": 1001,
        "wdr": 1,
        "zoomPosition": 0,
    }
    for key in ISP_ECHO_KEYS:
        if key in asked:
            reply[key] = asked[key]
    return reply


def _reported(requested: dict[str, Any], key: str, actual: Any) -> Any:
    """What to report for one field.

    The controller asks with `null` where it means *tell me what you are*. Echoing
    that null back is not an answer — it is stored verbatim, and a channel with no
    fps and no bitrate is a channel the controller will not stream from. So a null
    request falls through to the value we actually have.
    """
    value = requested.get(key)
    return actual if value is None else value


def video_settings(asked: dict[str, Any], armed: dict[str, str]) -> dict[str, Any]:
    """Report the video configuration, including which tracks are pushing where.

    `armed` maps a track name to the destination we accepted, so the controller
    sees its own instruction reflected back.
    """
    video: dict[str, Any] = {
        "enableHrd": False,
        "hdrMode": 0,
        "lowDelay": False,
        "videoMode": "default",
        "vinFps": 30,
        "videoCodec": identity.VIDEO_CODEC,
    }
    asked_video = asked.get("video")
    asked_video = asked_video if isinstance(asked_video, dict) else {}

    for channel in identity.CHANNELS:
        requested = asked_video.get(channel.track)
        requested = requested if isinstance(requested, dict) else {}
        serializer = requested.get("avSerializer")
        serializer = serializer if isinstance(serializer, dict) else {}
        destinations = serializer.get("destinations")
        destination = armed.get(channel.track, "")
        video[channel.track] = {
            "avSerializer": {
                "destinations": list(destinations)
                if isinstance(destinations, list)
                else ([destination] if destination else []),
                "parameters": serializer.get("parameters", {"streamName": channel.track}),
                "type": serializer.get("type", "extendedFlv"),
            },
            # An encoder that exists is an encoder that is on. The controller will
            # not ask a disabled channel for video.
            "enabled": True,
            "codec": identity.VIDEO_CODEC,
            "type": identity.VIDEO_CODEC,
            "bitRateCbrAvg": _reported(requested, "bitRateCbrAvg", channel.bitrate),
            "bitRateVbrMax": _reported(requested, "bitRateVbrMax", channel.bitrate),
            "bitRateVbrMin": _reported(requested, "bitRateVbrMin", channel.bitrate // 8),
            "fps": _reported(requested, "fps", channel.fps),
            "gopLength": _reported(requested, "gopLength", channel.fps * channel.idr_interval),
            "height": channel.height,
            "width": channel.width,
            "isCbr": _reported(requested, "isCbr", False),
            "maxFps": channel.fps,
            "minClientAdaptiveBitRate": _reported(
                requested, "minClientAdaptiveBitRate", channel.bitrate // 8
            ),
            "minMotionAdaptiveBitRate": _reported(
                requested, "minMotionAdaptiveBitRate", channel.bitrate // 8
            ),
            "nMultiplier": _reported(requested, "nMultiplier", 6),
            "name": channel.name.lower(),
            "streamId": channel.stream_id,
            "streaming": bool(destination),
            "validBitrateRangeMax": channel.bitrate,
            "validBitrateRangeMin": channel.bitrate // 16,
        }

    asked_audio = asked.get("audio")
    asked_audio = asked_audio if isinstance(asked_audio, dict) else {}
    return {
        "audio": {
            "bitRate": asked_audio.get("bitRate", 64_000),
            "channels": 1,
            "description": "audio track",
            "enableTemporalNoiseShaping": False,
            "enabled": True,
            "mode": 0,
            "quality": 0,
            "sampleRate": 16_000,
            "type": "aac",
            "volume": asked_audio.get("volume", 100),
        },
        "firmwarePath": "/lib/firmware/",
        "video": video,
    }


def device_settings(asked: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "name": asked.get("name", name),
        "timezone": asked.get("timezone", "UTC0"),
        "inactiveTimeout": 5_000,
        "isSshEnabled": False,
        "wdt": {"powerCycles": 0, "unexpectedResets": 0},
    }


def osd_settings(asked: dict[str, Any]) -> dict[str, Any]:
    """Echo the overlay configuration; the controller re-sends it on every change."""
    reply = {
        "enableOverlay": 1,
        "logoScale": 50,
        "overlayColorId": 0,
        "textScale": 50,
        "useCustomLogo": 0,
    }
    for slot in ("_1", "_2", "_3", "_4"):
        asked_slot = asked.get(slot)
        reply[slot] = asked_slot if isinstance(asked_slot, dict) else {  # type: ignore[assignment]
            "enableDate": 1,
            "enableLogo": 1,
            "enableReportdStatsLevel": 0,
            "enableStreamerStatsLevel": 0,
            "tag": "",
        }
    for key in ("enableOverlay", "logoScale", "overlayColorId", "textScale", "useCustomLogo"):
        if key in asked:
            reply[key] = asked[key]
    return reply


def sound_led_settings(asked: dict[str, Any]) -> dict[str, Any]:
    return {
        "ledFaceAlwaysOnWhenManaged": asked.get("ledFaceAlwaysOnWhenManaged", 1),
        "ledFaceEnabled": asked.get("ledFaceEnabled", 1),
        "systemSoundsEnabled": asked.get("systemSoundsEnabled", 1),
        "speakerVolume": asked.get("speakerVolume", 100),
    }


def network_status() -> dict[str, Any]:
    return {
        "connectionState": 2,
        "connectionStateDescription": "CONNECTED",
        "defaultInterface": "eth0",
        "dhcpLeasetime": 86_400,
        "dns1": "127.0.0.1",
        "dns2": "",
        "gateway": "0.0.0.0",
        "ipAddress": "0.0.0.0",
        "linkDuplex": 1,
        "linkSpeedMbps": 1000,
        "mode": "dhcp",
        "networkMask": "255.255.255.0",
    }


def analytics_settings(asked: dict[str, Any]) -> dict[str, Any]:
    """Echoed unchanged. On a real camera this verb resets things; here it is inert."""
    return dict(asked)


def generic_echo(asked: dict[str, Any]) -> dict[str, Any]:
    """Default answer: give back what was sent.

    A controller mostly wants to know the camera accepted the message. Echoing is
    both the least surprising answer and what the real camera does for the verbs
    we have no better information about.
    """
    return dict(asked)
