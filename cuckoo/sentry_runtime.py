"""Application wiring helpers for the Sentry idle patrol.

The patrol engine is intentionally independent of HTTP and Frigate.  This
module supplies the small amount of runtime plumbing needed by an embedding
application: a non-blocking movement gate, validated configuration parsing,
and a lifecycle wrapper.  Embedders should pass the same gate to manual,
autotracking, and return-home producers; a predicate check without this gate
is not sufficient to prevent a check-then-move race.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, ContextManager, Iterator, Mapping

from sentry import MIN_INTERVAL_SEC, PatrolPoint, SentryPatrol, SentrySnapshot


class MovementGate:
    """A non-blocking, observable lease shared by every PTZ command producer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: str | None = None
        self._acquired = 0
        self._rejected = 0

    @contextmanager
    def lease(self, owner: str = "unknown") -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            self._rejected += 1
            raise BlockingIOError("PTZ movement is reserved")
        self._owner = owner
        self._acquired += 1
        try:
            yield
        finally:
            self._owner = None
            self._lock.release()

    def snapshot(self) -> dict[str, object]:
        return {
            "busy": self._lock.locked(),
            "owner": self._owner,
            "acquired": self._acquired,
            "rejected": self._rejected,
        }


@dataclass(frozen=True)
class SentryConfig:
    enabled: bool = False
    interval_sec: float = 30.0
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
        raw_points = value.get("points", [])
        if not isinstance(raw_points, (list, tuple)):
            raise ValueError("sentry.points must be a list")
        points: list[PatrolPoint] = []
        for item in raw_points:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                raise ValueError("each sentry point needs a name")
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
            points.append(PatrolPoint(item["name"], dwell))
        return cls(enabled, interval_f, tuple(points))

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "interval_sec": self.interval_sec,
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
        goto_preset: Callable[[str], bool],
        tracking_active: Callable[[], bool],
        camera_idle: Callable[[], bool],
        gate: MovementGate | None = None,
    ) -> None:
        self.config = config
        self.gate = gate or MovementGate()
        self.patrol = SentryPatrol(
            goto_preset=goto_preset,
            tracking_active=tracking_active,
            camera_idle=camera_idle,
            interval_sec=config.interval_sec,
            movement_lease=lambda: self.gate.lease("sentry"),
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
