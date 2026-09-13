"""What finch claims to be.

Every value here was read out of the real camera — its `ubnt_avclient_hello` on
the wire, and its own record in the controller's bootstrap — rather than invented.
A controller decides what a camera can do from these fields, so a wrong one shows
up later as a missing feature rather than as an error.

The MAC is deliberately **not** the real camera's. Same Ubiquiti prefix so the
controller sees a plausible device, different tail so both can be adopted at once
and the real one is never disturbed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

MODEL: Final = "UVC G5 PTZ"
PLATFORM: Final = "sav530q"
HARDWARE_REVISION: Final = 17
SYSTEM_ID: Final = 0xA59B  # the G5 PTZ's model id
FIRMWARE: Final = "5.3.95"
FIRMWARE_BUILD: Final = "148b9a3.260612.645"
SEMVER: Final = "v5.3.95"
PROTOCOL_VERSION: Final = 67

UBIQUITI_OUI: Final = "28704E"
DEFAULT_MAC: Final = UBIQUITI_OUI + "F17C40"  # not the real camera's tail
# Never anything that reads like a real device: whatever adopts us should show
# plainly in its own UI that this camera is not real.
DEFAULT_NAME: Final = "finch virtual"

# Motor travel, in the units the camera reports. The controller reads its limits
# from here, so these have to match the real device or moves land in the wrong place.
PAN_STEPS: Final = (500, 35500)
TILT_STEPS: Final = (8000, 18000)
ZOOM_STEPS: Final = (0, 730)
FOCUS_STEPS: Final = (0, 255)

PAN_DEGREES: Final = (-175.0, 175.0)
TILT_DEGREES: Final = (-10.0, 90.0)

SMART_DETECT: Final = [
    "person",
    "vehicle",
    "animal",
    # These two are PTZ behaviours the camera advertises here, not sensor classes.
    "liveviewTracking",
    "autoTracking",
    "alrmSmoke",
    "alrmCmonx",
    "alrmBabyCry",
]
AUDIO_CODECS: Final = ["aac", "opus"]
AUDIO_STYLE: Final = ["nature", "noiseReduced"]
VIDEO_CODECS: Final = ["h264", "h265", "mjpg"]
VIDEO_CODEC: Final = "h265"  # what we actually send, and what the real G5 sends
VIDEO_MODES: Final = ["default", "sport", "slowShutter"]


@dataclass(frozen=True)
class Channel:
    """One encoder track, as the real camera is configured out of the box.

    `channel_id` and `stream_id` are not cosmetic: the receiver files recordings
    as `<MAC>_<channelId>`, so a stream that announces the wrong one is a stream
    nothing is waiting for.
    """

    name: str
    track: str
    channel_id: int
    stream_id: int
    width: int
    height: int
    fps: int
    bitrate: int
    idr_interval: int = 5


CHANNELS: Final[tuple[Channel, ...]] = (
    Channel("High", "video1", 0, 1, 2688, 1512, 30, 10_000_000),
    Channel("Medium", "video2", 1, 2, 1280, 720, 30, 2_000_000),
    Channel("Low", "video3", 2, 4, 640, 360, 30, 800_000),
)


def channel(track: str) -> Channel | None:
    return next((c for c in CHANNELS if c.track == track), None)


@dataclass
class Identity:
    """One camera's identity. Everything the controller is told about us."""

    mac: str = DEFAULT_MAC
    name: str = DEFAULT_NAME
    ip: str = "0.0.0.0"
    model: str = MODEL
    firmware: str = FIRMWARE
    started_at: float = field(default_factory=time.time)
    _guid: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def uptime(self) -> int:
        return int(time.time() - self.started_at)

    @property
    def mac_bytes(self) -> bytes:
        return bytes.fromhex(self.mac)

    @property
    def model_id(self) -> str:
        """What the camera puts in `camera-model`: the hex system id, not the name.

        The real G5 PTZ sends `0xa59b`. Sending the human-readable model string
        instead is enough to have the connection refused upstream.
        """
        return f"0x{SYSTEM_ID:04x}"

    @property
    def device_id(self) -> str:
        """A stable per-device UUID, as the camera sends in `device-id`."""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"finch-device-{self.mac}"))

    @property
    def guid(self) -> str:
        """A per-boot UUID, as the camera sends in `x-guid`."""
        return self._guid

    @property
    def mac_colons(self) -> str:
        return ":".join(self.mac[i : i + 2] for i in range(0, 12, 2)).lower()

    def features(self) -> dict[str, Any]:
        """The capability block in the camera's hello.

        This is the camera's own vocabulary — nested step ranges, `mic` as an
        integer, PTZ behaviours listed alongside sensor classes — and not the
        flattened `featureFlags` the controller derives from it.
        """
        return {
            "pan": {"steps": {"min": PAN_STEPS[0], "max": PAN_STEPS[1], "step": 9},
                    "degrees": {"min": PAN_DEGREES[0], "max": PAN_DEGREES[1], "step": 0.09}},
            "tilt": {"steps": {"min": TILT_STEPS[0], "max": TILT_STEPS[1], "step": 7},
                     "degrees": {"min": TILT_DEGREES[0], "max": TILT_DEGREES[1], "step": 0.07}},
            "zoom": {"steps": {"min": ZOOM_STEPS[0], "max": ZOOM_STEPS[1], "step": 2}, "ratio": 2},
            "focus": {"steps": {"min": FOCUS_STEPS[0], "max": FOCUS_STEPS[1], "step": 1}},
            "mic": 1,
            "smartDetect": list(SMART_DETECT),
            "audioCodecs": list(AUDIO_CODECS),
            "audioStyle": list(AUDIO_STYLE),
            "videoCodecs": list(VIDEO_CODECS),
            "videoMode": list(VIDEO_MODES),
            "motionDetect": ["enhanced"],
            "hasHdr": True,
            "hasWdr": True,
            "hasMic": True,
            "hasSpeaker": False,
            "hasInfrared": True,
            "hasMotionZones": True,
            "hasPrivacyMask": True,
            "isPtz": True,
        }

    def hello(self, token: str, host: str, port: int = 7442) -> dict[str, Any]:
        """The camera's opening message.

        `adoptionCode` is what makes a controller willing to take us on; the rest
        is identity the controller stores and shows.
        """
        return {
            "adoptionCode": token,
            "connectionHost": host,
            "connectionSecurePort": port,
            "fwVersion": self.firmware,
            "firmwareBuild": FIRMWARE_BUILD,
            "semver": SEMVER,
            "hwaddr": self.mac_colons,
            "mac": self.mac,
            "hwrev": HARDWARE_REVISION,
            "sysid": SYSTEM_ID,
            "platform": PLATFORM,
            "model": self.model,
            "lensmodel": self.model,
            "name": self.name,
            "cameraName": self.name,
            "ip": self.ip,
            "protocolVersion": PROTOCOL_VERSION,
            "rebootTimeoutSec": 30,
            "upgradeTimeoutSec": 150,
            "uptime": self.uptime,
            "idleTime": float(self.uptime),
            "totalLoad": 0.54,
            "isGen5s": True,
            "isDoorbellSeries": False,
            "features": self.features(),
        }

    def param_agreement(self, token: str) -> dict[str, Any]:
        """Answer to the controller's paramAgreement. It wants the token back."""
        return {"authToken": token, "features": self.features()}
