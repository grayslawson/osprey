"""Safe idle-time PTZ patrol scheduling.

Sentry is deliberately controller-agnostic.  The host application supplies a
named-preset callback and live camera/tracking predicates.  The optional lease
callback must be shared with every other movement producer (autotracking,
return-home, and manual control) when integrated; checking a predicate alone
cannot close a check-then-move race.

The patrol moves under the ``PATROL`` arbitration source.  It yields to a
person exactly as an autotracker does, but it is a distinct kind so the
controller can tell an autotracker apart from a patrol: a patrol has to stand
down while an autotracker is driving, and one that mistook its own moves for
tracking would never advance.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable, ContextManager, Final

import arbitration

log = logging.getLogger("osprey.sentry")

MIN_INTERVAL_SEC: Final = 1.0
# The patrol hold and dwell overrides are bounded so a sidecar cannot starve the patrol.
MAX_HOLD_SEC: Final = 300.0
MAX_DWELL_FACTOR: Final = 10.0
MAX_DWELL_TTL_SEC: Final = 86400.0
# A day/night change must persist this long before the patrol switches views, so
# a flickering scene state cannot thrash the head.
NIGHT_HOLD_SEC: Final = 120.0


@dataclass(frozen=True)
class PatrolPoint:
    """A user-visible named camera preset."""

    name: str
    dwell_sec: float | None = None
    # Preset to visit instead of ``name`` while the camera reports NIGHT, e.g. a
    # view that avoids IR bounce off a nearby wall. Missing preset: ``name`` is used.
    night_preset: str | None = None


@dataclass(frozen=True)
class SentrySnapshot:
    enabled: bool
    state: str
    point: str | None
    points: tuple[str, ...]
    next_move_at: float | None
    last_error: str | None
    moves: int
    hold_remaining_sec: float = 0.0
    hold_reason: str = ""
    dwell_override: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "point": self.point,
            "points": list(self.points),
            "next_move_at": self.next_move_at,
            "last_error": self.last_error,
            "moves": self.moves,
            "hold_remaining_sec": round(self.hold_remaining_sec, 1),
            "hold_reason": self.hold_reason,
            "dwell_override": dict(self.dwell_override),
        }


@dataclass
class SentryPatrol:
    """Rotate named presets only while tracking and camera motion are idle.

    ``goto_preset`` should return only after the command is accepted; the next
    cycle is scheduled after the configured dwell.  ``movement_lease`` is a
    context manager factory.  A lease that cannot be acquired should raise
    ``BlockingIOError`` so the move is deferred without consuming a point.
    """

    goto_preset: Callable[[str], bool]
    tracking_active: Callable[[], bool]
    camera_idle: Callable[[], bool]
    interval_sec: float = 30.0
    movement_lease: Callable[[], ContextManager[object]] | None = None
    is_night: Callable[[], bool] | None = None
    night_hold_sec: float = NIGHT_HOLD_SEC
    on_status: Callable[[SentrySnapshot], None] | None = None
    _points: list[PatrolPoint] = field(default_factory=list, init=False)
    _enabled: bool = field(default=False, init=False)
    _index: int = field(default=0, init=False)
    _next_move_at: float | None = field(default=None, init=False)
    _state: str = field(default="disabled", init=False)
    _point: str | None = field(default=None, init=False)
    _last_error: str | None = field(default=None, init=False)
    _moves: int = field(default=0, init=False)
    _hold_until: float = field(default=0.0, init=False)
    _hold_reason: str = field(default="", init=False)
    _dwell_override: dict[str, float] = field(default_factory=dict, init=False)
    _dwell_until: float = field(default=0.0, init=False)
    _night: bool = field(default=False, init=False)
    _night_changed_at: float | None = field(default=None, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.interval_sec = max(MIN_INTERVAL_SEC, float(self.interval_sec))

    def configure(self, points: list[PatrolPoint] | tuple[PatrolPoint, ...]) -> None:
        names: set[str] = set()
        clean: list[PatrolPoint] = []
        for point in points:
            name = point.name.strip()
            if not name or name.casefold() in names:
                raise ValueError("patrol point names must be non-empty and unique")
            dwell = self.interval_sec if point.dwell_sec is None else float(point.dwell_sec)
            if dwell < MIN_INTERVAL_SEC:
                raise ValueError(f"dwell must be at least {MIN_INTERVAL_SEC:g} seconds")
            names.add(name.casefold())
            night = point.night_preset.strip() if point.night_preset else None
            clean.append(PatrolPoint(name, dwell, night or None))
        with self._lock:
            self._points = clean
            self._index = 0
            self._point = None
            if not clean:
                self._enabled = False
                self._state = "disabled"
                self._next_move_at = None
            self._publish()

    def enable(self, *, now: float | None = None) -> None:
        with self._lock:
            if not self._points:
                raise ValueError("configure at least one patrol point first")
            self._enabled = True
            self._state = "waiting"
            self._last_error = None
            # Start with the first point after the interval, avoiding a sudden
            # move immediately when an operator enables patrol.
            self._next_move_at = (time.monotonic() if now is None else now) + self.interval_sec
            self._publish()

    def disable(self) -> None:
        with self._lock:
            self._enabled = False
            self._next_move_at = None
            self._state = "disabled"
            self._publish()

    # Runtime-only overrides for Osprey Assist. Both expire on their own, so a
    # crashed sidecar can never freeze or reshape the patrol, and neither is
    # persisted: the configured tour stays the source of truth.

    def hold(self, seconds: float, reason: str = "", *, now: float | None = None) -> float:
        """Stand down for ``seconds``; returns the expiry on the patrol clock."""
        clock = time.monotonic() if now is None else now
        with self._lock:
            self._hold_until = clock + seconds
            self._hold_reason = reason[:64]
            self._publish()
            return self._hold_until

    def release(self) -> None:
        with self._lock:
            self._hold_until = 0.0
            self._hold_reason = ""
            self._publish()

    def hold_remaining(self, *, now: float | None = None) -> float:
        clock = time.monotonic() if now is None else now
        with self._lock:
            return max(0.0, self._hold_until - clock)

    def set_dwell(self, dwell: dict[str, float], ttl_sec: float, *, now: float | None = None) -> dict[str, float]:
        """Override per-point dwell for ``ttl_sec``. Unknown points are ignored.

        Each value is clamped to ``MIN_INTERVAL_SEC`` .. ``interval_sec * MAX_DWELL_FACTOR``.
        Returns what was applied, keyed by the configured point name.
        """
        clock = time.monotonic() if now is None else now
        with self._lock:
            known = {point.name.casefold(): point.name for point in self._points}
            high = self.interval_sec * MAX_DWELL_FACTOR
            applied = {
                known[key.strip().casefold()]: min(high, max(MIN_INTERVAL_SEC, float(value)))
                for key, value in dwell.items() if key.strip().casefold() in known
            }
            self._dwell_override = {name.casefold(): value for name, value in applied.items()}
            self._dwell_until = clock + ttl_sec if applied else 0.0
            return applied

    def dwell_override(self, *, now: float | None = None) -> dict[str, float]:
        clock = time.monotonic() if now is None else now
        with self._lock:
            if clock >= self._dwell_until:
                self._dwell_override = {}
            return {name: value for name, value in self._dwell_override.items()}

    def snapshot(self) -> SentrySnapshot:
        with self._lock:
            return SentrySnapshot(
                self._enabled, self._state, self._point,
                tuple(point.name for point in self._points), self._next_move_at,
                self._last_error, self._moves,
                max(0.0, self._hold_until - time.monotonic()), self._hold_reason,
                dict(self._dwell_override) if time.monotonic() < self._dwell_until else {},
            )

    def _night_now(self, clock: float) -> bool:
        """The scene state, switched only after it has held for ``night_hold_sec``."""
        if self.is_night is None:
            return False
        raw = bool(self.is_night())
        if self._night_changed_at is None:  # first reading is taken as is
            self._night, self._night_changed_at = raw, clock
        elif raw != self._night and clock - self._night_changed_at >= self.night_hold_sec:
            self._night, self._night_changed_at = raw, clock
        elif raw == self._night:
            self._night_changed_at = clock  # a blip back resets the pending change
        return self._night

    def step(self, *, now: float | None = None) -> bool:
        """Run one due patrol decision; return True only when a move was sent."""
        clock = time.monotonic() if now is None else now
        with self._lock:
            if self._enabled and clock < self._hold_until:
                # Assist is running a trial. The schedule is left alone, so the
                # patrol resumes the moment the hold ends.
                if self._state != "assist":
                    self._state = "assist"
                    self._publish()
                return False
            if self._state == "assist":
                self._state = "waiting"
                self._publish()
            if not self._enabled or not self._points or self._next_move_at is None or clock < self._next_move_at:
                return False
            if self.tracking_active():
                self._state, self._next_move_at = "tracking", clock + 1.0
                log.debug("sentry patrol stands down: an autotracker is driving this camera")
                self._publish()
                return False
            if not self.camera_idle():
                self._state, self._next_move_at = "camera_busy", clock + 1.0
                log.debug("sentry patrol stands down: the camera is still moving")
                self._publish()
                return False
            point = self._points[self._index]
            lease = self.movement_lease() if self.movement_lease else nullcontext()
            try:
                with lease:
                    # Re-check inside the shared lease: this is the important
                    # protection against Frigate becoming active after the
                    # initial checks.
                    if self.tracking_active() or not self.camera_idle():
                        self._state, self._next_move_at = "busy", clock + 1.0
                        self._publish()
                        return False
                    token = arbitration.source.set(arbitration.PATROL)  # a patrol yields to people
                    try:
                        wanted = [point.name]
                        if point.night_preset and self._night_now(clock):
                            wanted.insert(0, point.night_preset)  # a refused night view falls back
                        for target in wanted:
                            accepted = self.goto_preset(target)
                            if accepted:
                                break
                    finally:
                        arbitration.source.reset(token)
            except BlockingIOError:
                self._state, self._next_move_at = "busy", clock + 1.0
                self._publish()
                return False
            except Exception as exc:  # callbacks cross network/controller boundaries
                self._last_error = str(exc) or type(exc).__name__
                self._state, self._next_move_at = "error", clock + 1.0
                log.warning("sentry patrol move to %s failed: %s", point.name, exc)
                self._publish()
                return False
            if not accepted:
                self._last_error = f"preset {point.name!r} was refused"
                self._state, self._next_move_at = "error", clock + 1.0
                self._publish()
                return False
            self._last_error = None
            self._point = target
            self._moves += 1
            self._index = (self._index + 1) % len(self._points)
            dwell = point.dwell_sec if point.dwell_sec is not None else self.interval_sec
            if clock < self._dwell_until:
                dwell = self._dwell_override.get(point.name.casefold(), dwell)
            self._state, self._next_move_at = "moving", clock + dwell
            log.info("sentry patrol moved to %s", point.name)
            self._publish()
            return True

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="sentry-patrol", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.disable()

    def _run(self) -> None:
        while not self._stop.wait(0.25):
            try:
                self.step()
            except Exception:  # defensive: scheduler must never die silently
                log.exception("unexpected sentry scheduler failure")

    def _publish(self) -> None:
        if self.on_status:
            self.on_status(self.snapshot())
