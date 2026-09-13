"""Device model — the seam between the ONVIF front end and the camera controller.

Nothing here knows about SOAP or about wire message names. Both sides depend on
this and not on each other.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

PAN: Final = "pan"
TILT: Final = "tilt"
ZOOM: Final = "zoom"
FOCUS: Final = "focus"

AXES: Final[tuple[str, str, str, str]] = (PAN, TILT, ZOOM, FOCUS)
MOTION_EVENT_TIMEOUT_SEC: Final = 60.0


class Codec(Enum):
    H264 = "h264"
    H265 = "h265"
    MJPEG = "mjpg"
    AAC = "aac"
    OPUS = "opus"


@dataclass(frozen=True)
class AxisRange:
    """Motor travel for one axis, in raw motor units as the camera reports them."""

    minimum: int
    maximum: int
    invert: bool = False

    def clamp(self, value: int) -> int:
        return max(self.minimum, min(self.maximum, value))

    def to_normalised(self, value: int) -> float:
        """Map motor units onto ONVIF's -1.0..1.0."""
        span = self.maximum - self.minimum
        if span <= 0:
            return 0.0
        unit = (self.clamp(value) - self.minimum) / span
        out = unit * 2.0 - 1.0
        return -out if self.invert else out

    def from_normalised(self, value: float) -> int:
        v = -value if self.invert else value
        unit = (max(-1.0, min(1.0, v)) + 1.0) / 2.0
        return self.clamp(round(self.minimum + unit * (self.maximum - self.minimum)))


@dataclass(frozen=True)
class Position:
    pan: int = 0
    tilt: int = 0
    zoom: int = 0
    focus: int = 0

    def as_dict(self) -> dict[str, int]:
        return {PAN: self.pan, TILT: self.tilt, ZOOM: self.zoom, FOCUS: self.focus}


@dataclass(frozen=True)
class FieldOfView:
    """Measured lens and motor geometry for ONVIF FOV-relative movement."""

    pan_degrees: float
    tilt_degrees: float
    wide_horizontal_degrees: float
    wide_vertical_degrees: float
    tele_horizontal_degrees: float
    tele_vertical_degrees: float

    def at_zoom(self, zoom: float) -> tuple[float, float]:
        """Approximate current FOV by interpolating the measured lens endpoints."""
        factor = max(0.0, min(1.0, zoom))
        horizontal = self.wide_horizontal_degrees + factor * (
            self.tele_horizontal_degrees - self.wide_horizontal_degrees
        )
        vertical = self.wide_vertical_degrees + factor * (
            self.tele_vertical_degrees - self.wide_vertical_degrees
        )
        return horizontal, vertical


# Ubiquiti's published UVC G5 PTZ mechanical travel and wide/tele lens angles.
# The hexadecimal lens model is what the camera-facing protocol reports on some
# firmware versions. Other PTZ models must get their own measured calibration;
# silently applying this geometry would make TranslationSpaceFov untruthful.
G5_PTZ_FIELD_OF_VIEW: Final = FieldOfView(
    pan_degrees=350.0,
    tilt_degrees=100.0,
    wide_horizontal_degrees=99.7,
    wide_vertical_degrees=51.9,
    tele_horizontal_degrees=45.5,
    tele_vertical_degrees=25.4,
)
G5_PTZ_MODELS: Final[frozenset[str]] = frozenset({"uvc g5 ptz", "0xa59b"})


@dataclass
class Preset:
    index: int
    name: str
    position: Position


@dataclass(frozen=True)
class VideoTrack:
    """One encoder track the camera can be told to push."""

    name: str
    stream_id: int
    source_id: int
    width: int
    height: int
    fps: int
    codec: Codec
    bitrate: int

    @property
    def is_video(self) -> bool:
        return self.codec in (Codec.H264, Codec.H265, Codec.MJPEG)


@dataclass
class Motion:
    """Latest known gimbal state.

    Position arrives two ways: polled, and pushed while the head is moving.
    Both write here; `settled` reflects the most recent push.
    """

    position: Position = field(default_factory=Position)
    activity: int = 0
    updated_at: float = 0.0
    available: bool = False
    _target: Position | None = field(default=None, init=False, repr=False)
    _seen_activity: bool = field(default=False, init=False, repr=False)
    _deadline: float = field(default=0.0, init=False, repr=False)
    _error: str | None = field(default="PTZ channel unavailable", init=False, repr=False)
    _pan_tilt_pending: bool = field(default=False, init=False, repr=False)
    _zoom_pending: bool = field(default=False, init=False, repr=False)
    _connected_once: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    @property
    def settled(self) -> bool:
        with self._lock:
            self._expire_if_needed()
            return self.activity == 0

    @property
    def status(self) -> str:
        """The ONVIF state, including an honest UNKNOWN while PTZ is unavailable."""
        with self._lock:
            self._expire_if_needed()
            if not self.available or self._error is not None:
                return "UNKNOWN"
            return "MOVING" if self._pan_tilt_pending or self._zoom_pending else "IDLE"

    def _expire_if_needed(self) -> None:
        if (
            self.activity != 0
            and self._deadline > 0.0
            and time.monotonic() >= self._deadline
        ):
            self.activity = 0
            self._target = None
            self._seen_activity = False
            self._deadline = 0.0
            self._error = "motor state timeout"
            self._pan_tilt_pending = False
            self._zoom_pending = False

    def connect(self) -> None:
        with self._lock:
            self.available = True
            self._error = None
            self._connected_once = True

    def open_channel(self) -> bool:
        """Open a PTZ socket and return whether it replaced an earlier socket."""
        with self._lock:
            reconnecting = self._connected_once
        if reconnecting:
            self.reconnect()
        else:
            self.connect()
        return reconnecting

    def reconnect(self) -> None:
        """Discard an in-flight command when a new PTZ socket takes custody.

        A replacement socket cannot complete the old socket's command contract.
        Keep status UNKNOWN until the position request sent with the new channel
        produces a fresh motor-state reply.
        """
        with self._lock:
            self.available = True
            self._connected_once = True
            self.activity = 0
            self._target = None
            self._seen_activity = False
            self._deadline = 0.0
            self._error = "awaiting PTZ state"
            self._pan_tilt_pending = False
            self._zoom_pending = False
            self.updated_at = time.time()

    def disconnect(self) -> None:
        with self._lock:
            self.available = False
            self.activity = 0
            self._target = None
            self._seen_activity = False
            self._deadline = 0.0
            self._error = "PTZ channel unavailable"
            self._pan_tilt_pending = False
            self._zoom_pending = False
            self.updated_at = time.time()

    def begin(
        self, target: Position | None, timeout: float = MOTION_EVENT_TIMEOUT_SEC
    ) -> bool:
        """Atomically reserve one move; overlapping commands are rejected."""
        with self._lock:
            self._expire_if_needed()
            if not self.available or self.activity != 0:
                return False
            self.activity = 1
            self._target = target
            self._seen_activity = False
            self._deadline = time.monotonic() + max(0.0, timeout)
            self._error = None
            if target is None:
                # A previously stored camera preset does not expose its axes.
                self._pan_tilt_pending = True
                self._zoom_pending = True
            else:
                self._set_pending_axes(target, self.position)
            self.updated_at = time.time()
            return True

    def cancel(self) -> None:
        """Roll back a command that could not be written to the PTZ channel."""
        with self._lock:
            self.activity = 0
            self._target = None
            self._seen_activity = False
            self._deadline = 0.0
            self._error = None
            self._pan_tilt_pending = False
            self._zoom_pending = False
            self.updated_at = time.time()

    def _set_pending_axes(self, target: Position, current: Position) -> None:
        self._pan_tilt_pending = (
            target.pan != current.pan or target.tilt != current.tilt
        )
        self._zoom_pending = target.zoom != current.zoom

    def snapshot(self) -> tuple[Position, str, str, str | None]:
        with self._lock:
            self._expire_if_needed()
            if not self.available or self._error is not None:
                pan_tilt_state = "UNKNOWN"
                zoom_state = "UNKNOWN"
            else:
                pan_tilt_state = "MOVING" if self._pan_tilt_pending else "IDLE"
                zoom_state = "MOVING" if self._zoom_pending else "IDLE"
            return self.position, pan_tilt_state, zoom_state, self._error

    def update(self, position: Position, activity: int) -> None:
        with self._lock:
            self._error = None
            self.position = position
            if self._target is not None:
                self._set_pending_axes(self._target, position)
            # A zero-activity update for an older poll or superseded position must
            # not settle the command currently reserved by begin(). The camera's
            # terminal event carries the exact integer target we sent.
            if activity != 0:
                self._seen_activity = True
                self.activity = activity
                if self._target is None:
                    self._pan_tilt_pending = True
                    self._zoom_pending = True
                self._deadline = time.monotonic() + MOTION_EVENT_TIMEOUT_SEC
            elif (
                self._target is None and self._seen_activity
            ) or position == self._target:
                self.activity = activity
                self._target = None
                self._seen_activity = False
                self._deadline = 0.0
                self._pan_tilt_pending = False
                self._zoom_pending = False
            self.updated_at = time.time()


@dataclass
class Camera:
    """Everything the front end may know about the adopted camera."""

    mac: str
    model: str = ""
    firmware: str = ""
    name: str = "cuckoo camera"
    adopted: bool = False
    pan_range: AxisRange = field(default_factory=lambda: AxisRange(0, 0))
    tilt_range: AxisRange = field(default_factory=lambda: AxisRange(0, 0))
    zoom_range: AxisRange = field(default_factory=lambda: AxisRange(0, 0))
    tracks: list[VideoTrack] = field(default_factory=list)
    presets: dict[int, Preset] = field(default_factory=dict)
    motion: Motion = field(default_factory=Motion)
    smart_detect: list[str] = field(default_factory=list)
    audio_codecs: list[Codec] = field(default_factory=list)

    @property
    def is_ptz(self) -> bool:
        return self.pan_range.maximum > self.pan_range.minimum

    @property
    def field_of_view(self) -> FieldOfView | None:
        """Return geometry only for camera identities with a known calibration."""
        if self.model.strip().casefold() in G5_PTZ_MODELS:
            return G5_PTZ_FIELD_OF_VIEW
        return None

    def range_for(self, axis: str) -> AxisRange:
        if axis == PAN:
            return self.pan_range
        if axis == TILT:
            return self.tilt_range
        if axis == ZOOM:
            return self.zoom_range
        raise KeyError(axis)

    def track(self, name: str) -> VideoTrack | None:
        return next((t for t in self.tracks if t.name == name), None)

    def next_preset_index(self) -> int:
        return max(self.presets, default=-1) + 1
