"""Application wiring helpers for the Sentry idle patrol.

The patrol engine is intentionally independent of HTTP and Frigate.  This
module supplies the small amount of runtime plumbing needed by an embedding
application: a non-blocking movement gate, validated configuration parsing,
and a lifecycle wrapper.  Embedders should pass the same gate to manual,
autotracking, and return-home producers; a predicate check without this gate
is not sufficient to prevent a check-then-move race.

The gate is keyed by camera.  A controller that serves more than one camera
runs one patrol per camera, and one camera's patrol must not block another's.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Mapping

import ptz
from sentry import MIN_INTERVAL_SEC, PatrolPoint, SentryPatrol, SentrySnapshot

DEFAULT_TRACKING_HOLD_SEC: float = 15.0


class MovementGate:
    """A non-blocking, observable lease shared by every PTZ command producer.

    The lease is held per camera key, so a patrol on one camera cannot block a
    patrol on another.  The empty key is the unscoped producer — the default
    camera, used by the console and by MQTT commands that name no camera.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._owners: dict[str, str | None] = {}
        self._acquired = 0
        self._rejected = 0

    def _lock_for(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def lease(self, owner: str = "unknown", key: str = "") -> Iterator[None]:
        """Hold the movement lease for one camera.

        Raises ``BlockingIOError`` when another producer already holds it, so
        the caller can defer instead of waiting.
        """
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            with self._lock:
                self._rejected += 1
            raise BlockingIOError("PTZ movement is reserved")
        try:
            with self._lock:
                self._owners[key] = owner
                self._acquired += 1
            yield
        finally:
            # The owner is cleared and the lock released together, so a
            # snapshot never shows a lease held by nobody.
            with self._lock:
                self._owners[key] = None
                lock.release()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            owners = [owner for owner in self._owners.values() if owner is not None]
            cameras = {
                key: {"busy": lock.locked(), "owner": self._owners.get(key)}
                for key, lock in self._locks.items()
            }
            return {
                "busy": any(lock.locked() for lock in self._locks.values()),
                "owner": owners[0] if owners else None,
                "acquired": self._acquired,
                "rejected": self._rejected,
                "cameras": cameras,
            }


@dataclass(frozen=True)
class SentryConfig:
    """One camera's patrol: when it runs, how fast, and where it goes."""

    enabled: bool = False
    interval_sec: float = 30.0
    speed: int = ptz.DEFAULT_SPEED
    tracking_hold_sec: float = DEFAULT_TRACKING_HOLD_SEC
    points: tuple[PatrolPoint, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> "SentryConfig":
        value = raw or {}
        enabled = value.get("enabled", False)
        interval = value.get("interval_sec", 30.0)
        if not isinstance(enabled, bool):
            raise ValueError("sentry.enabled must be boolean")
        if isinstance(interval, bool):
            raise ValueError("sentry.interval_sec must be a number")
        try:
            interval_f = float(interval) if isinstance(interval, (int, float, str)) else 0.0
        except (TypeError, ValueError) as exc:
            raise ValueError("sentry.interval_sec must be a number") from exc
        if interval_f < MIN_INTERVAL_SEC:
            raise ValueError(f"sentry.interval_sec must be at least {MIN_INTERVAL_SEC:g}")

        speed = value.get("speed", ptz.DEFAULT_SPEED)
        if isinstance(speed, bool) or not isinstance(speed, (int, str)):
            raise ValueError("sentry.speed must be an integer")
        try:
            speed_i = int(speed)
        except (TypeError, ValueError) as exc:
            raise ValueError("sentry.speed must be an integer") from exc
        if not ptz.MIN_SPEED <= speed_i <= ptz.MAX_SPEED_LIMIT:
            raise ValueError(
                f"sentry.speed must be an integer from {ptz.MIN_SPEED} to {ptz.MAX_SPEED_LIMIT}"
            )

        hold = value.get("tracking_hold_sec", DEFAULT_TRACKING_HOLD_SEC)
        if isinstance(hold, bool):
            raise ValueError("sentry.tracking_hold_sec must be a number")
        try:
            hold_f = float(hold) if isinstance(hold, (int, float, str)) else 0.0
        except (TypeError, ValueError) as exc:
            raise ValueError("sentry.tracking_hold_sec must be a number") from exc
        if hold_f < MIN_INTERVAL_SEC:
            raise ValueError(
                f"sentry.tracking_hold_sec must be at least {MIN_INTERVAL_SEC:g}"
            )

        raw_points = value.get("points", [])
        if not isinstance(raw_points, (list, tuple)):
            raise ValueError("sentry.points must be a list")
        points: list[PatrolPoint] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_points):
            if not isinstance(item, Mapping):
                raise ValueError(f"sentry.points[{index}] must be an object")
            unknown = set(item) - {"name", "dwell_sec"}
            if unknown:
                raise ValueError(
                    f"sentry.points[{index}] has unexpected field(s): {', '.join(sorted(unknown))}"
                )
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"sentry.points[{index}].name must be a non-empty string")
            key = name.strip().casefold()
            if key in seen:
                raise ValueError(f"sentry.points repeats the point {name.strip()!r}")
            seen.add(key)
            dwell_raw = item.get("dwell_sec")
            if dwell_raw is not None and isinstance(dwell_raw, bool):
                raise ValueError("sentry point dwell_sec must be a number")
            if dwell_raw is None:
                dwell = None
            elif isinstance(dwell_raw, (int, float, str)):
                try:
                    dwell = float(dwell_raw)
                except ValueError as exc:
                    raise ValueError("sentry point dwell_sec must be a number") from exc
            else:
                raise ValueError("sentry point dwell_sec must be a number")
            if dwell is not None and dwell < MIN_INTERVAL_SEC:
                raise ValueError(
                    f"sentry point dwell_sec must be at least {MIN_INTERVAL_SEC:g} seconds"
                )
            points.append(PatrolPoint(name.strip(), dwell))
        return cls(enabled, interval_f, speed_i, hold_f, tuple(points))

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "interval_sec": self.interval_sec,
            "speed": self.speed,
            "tracking_hold_sec": self.tracking_hold_sec,
            "points": [
                {"name": point.name, **({"dwell_sec": point.dwell_sec} if point.dwell_sec is not None else {})}
                for point in self.points
            ],
        }


class SentryRuntime:
    """Lifecycle and status boundary for a configured Sentry patrol."""

    def __init__(
        self,
        config: SentryConfig,
        goto_preset: Callable[[str, int], bool],
        tracking_active: Callable[[], bool],
        camera_idle: Callable[[], bool],
        gate: MovementGate | None = None,
        camera_key: str = "",
    ) -> None:
        self.config = config
        self.gate = gate or MovementGate()
        self.camera_key = camera_key
        self.patrol = SentryPatrol(
            goto_preset=lambda name: goto_preset(name, config.speed),
            tracking_active=tracking_active,
            camera_idle=camera_idle,
            interval_sec=config.interval_sec,
            movement_lease=lambda: self.gate.lease("sentry", self.camera_key),
        )
        self.patrol.configure(list(config.points))
        if config.enabled and config.points:
            self.patrol.enable()

    def start(self) -> None:
        self.patrol.start()

    def stop(self) -> None:
        self.patrol.stop()

    def snapshot(self) -> dict[str, object]:
        return {"config": self.config.as_dict(), "state": self.patrol.snapshot().as_dict(), "movement": self.gate.snapshot()}


__all__ = [
    "DEFAULT_TRACKING_HOLD_SEC",
    "MovementGate",
    "SentryConfig",
    "SentryRuntime",
    "SentrySnapshot",
]
