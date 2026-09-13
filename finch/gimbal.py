"""A head that isn't there, moving as though it were.

The controller drives motion by writing a preset and then telling us to go to it,
and it watches two things while we travel: the `EventMotorState` broadcasts we
push, and the `GetCurrentPosition` replies it polls for. Both have to agree, and
the last broadcast has to land exactly on the requested position — that is what a
controller treats as "arrived".

So this is a motor model, not an animation: it steps toward a target at a speed,
reports where it is, and stops when it gets there.
"""

from __future__ import annotations

import time
from _thread import RLock
from dataclasses import dataclass, field
from typing import Any, Final

import identity

# Measured on the real camera: a flag word, not a magnitude. 0 means settled.
ACTIVITY_SETTLED: Final = 0
ACTIVITY_MOVING: Final = 16

# The camera reports which coordinate system it is using; a consumer must not
# assume. We report the one the real camera reported.
SCALE: Final = "normalized"
FOCUS_MODE: Final = "manual"

DEFAULT_SPEED: Final = 1000
# Speed is in the controller's own units. This turns it into motor units per
# second, chosen so a full pan sweep at speed 1000 takes about six seconds —
# close to the real head, and slow enough that a watcher sees the travel.
UNITS_PER_SPEED_SECOND: Final = 6.0


@dataclass
class Axis:
    """One motor: where it is, where it is going, and how fast."""

    minimum: int
    maximum: int
    position: float

    def clamp(self, value: float) -> float:
        return max(float(self.minimum), min(float(self.maximum), value))

    def advance(self, target: float, units: float) -> bool:
        """Move up to `units` toward the target. True once there."""
        target = self.clamp(target)
        gap = target - self.position
        if abs(gap) <= units or units <= 0:
            self.position = target
            return True
        self.position += units if gap > 0 else -units
        return False


@dataclass
class Gimbal:
    """The whole head. Thread-safe enough for one mover and many readers."""

    pan: Axis = field(default_factory=lambda: Axis(*identity.PAN_STEPS, identity.PAN_STEPS[0]))
    tilt: Axis = field(default_factory=lambda: Axis(*identity.TILT_STEPS, identity.TILT_STEPS[0]))
    zoom: Axis = field(default_factory=lambda: Axis(*identity.ZOOM_STEPS, identity.ZOOM_STEPS[0]))
    focus: Axis = field(default_factory=lambda: Axis(*identity.FOCUS_STEPS, 58.0))
    target: dict[str, float] = field(default_factory=dict)
    speed: int = DEFAULT_SPEED
    presets: dict[int, dict[str, int]] = field(default_factory=dict)
    _last_step: float = field(default_factory=time.time)
    _lock: RLock = field(default_factory=RLock, repr=False)

    @property
    def axes(self) -> dict[str, Axis]:
        return {"pan": self.pan, "tilt": self.tilt, "zoom": self.zoom, "focus": self.focus}

    @property
    def moving(self) -> bool:
        with self._lock:
            return bool(self.target)

    def position(self) -> dict[str, int]:
        return self.snapshot()

    def snapshot(self) -> dict[str, int]:
        """Read all motor coordinates atomically for the video renderer."""
        with self._lock:
            return {name: int(round(axis.position)) for name, axis in self.axes.items()}

    # ------------------------------------------------------------------ presets

    def configure(self, items: list[dict[str, Any]]) -> None:
        """Store a preset. An arbitrary move is written to a scratch slot first."""
        with self._lock:
            for item in items:
                index = item.get("index")
                if not isinstance(index, int):
                    continue
                self.presets[index] = {
                    axis: int(item[axis])
                    for axis in self.axes
                    if isinstance(item.get(axis), (int, float))
                }

    def go(self, index: int, speed: int = DEFAULT_SPEED) -> bool:
        """Start travelling to a stored preset. False if we have never been told it."""
        with self._lock:
            preset = self.presets.get(index)
            if preset is None:
                return False
            self.speed = max(1, speed)
            self.target = {axis: float(value) for axis, value in preset.items()}
            self._last_step = time.time()
            return True

    def stop(self) -> None:
        with self._lock:
            self.target = {}

    # ------------------------------------------------------------------- motion

    def step(self, now: float | None = None) -> bool:
        """Advance by however long has passed. True while still moving."""
        with self._lock:
            if not self.target:
                return False
            now = now if now is not None else time.time()
            elapsed = max(0.0, now - self._last_step)
            self._last_step = now
            units = self.speed * UNITS_PER_SPEED_SECOND * elapsed
            arrived = True
            for name, want in self.target.items():
                axis = self.axes.get(name)
                if axis is None:
                    continue
                if not axis.advance(want, units):
                    arrived = False
            if arrived:
                self.target = {}
            return not arrived

    # ------------------------------------------------------------- what we say

    def state(self) -> dict[str, Any]:
        """The `state` block, shaped as the real camera reports it."""
        return {
            "activity": ACTIVITY_MOVING if self.moving else ACTIVITY_SETTLED,
            "focusMode": FOCUS_MODE,
            "scale": SCALE,
            "position": self.position(),
            "wallClockMs": int(time.time() * 1000),
        }

    def motor_state(self) -> dict[str, Any]:
        """An `EventMotorState` payload."""
        return {"ignoreActivity": True, "state": self.state()}

    def current_position(self, in_degree: bool = True, in_steps: bool = True) -> dict[str, Any]:
        """A `GetCurrentPosition` reply.

        The request asks for both unit systems explicitly, so both are answered.
        Degrees are derived from the announced step-to-degree ranges rather than
        invented. [INFERRED — the request shape is measured, this reply is not]
        """
        payload: dict[str, Any] = {"scale": SCALE, "focusMode": FOCUS_MODE}
        if in_steps:
            payload["position"] = self.position()
        if in_degree:
            payload["degrees"] = {
                "pan": _to_degrees(self.pan, identity.PAN_DEGREES),
                "tilt": _to_degrees(self.tilt, identity.TILT_DEGREES),
            }
        payload["activity"] = ACTIVITY_MOVING if self.moving else ACTIVITY_SETTLED
        return payload


def _to_degrees(axis: Axis, degrees: tuple[float, float]) -> float:
    span = axis.maximum - axis.minimum
    if span <= 0:
        return 0.0
    unit = (axis.position - axis.minimum) / span
    return round(degrees[0] + unit * (degrees[1] - degrees[0]), 2)
