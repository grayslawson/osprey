"""PTZ arbitration between people and autotrackers: manual wins, then resume.

Every PTZ command is tagged with the kind of client that sent it, through the
``source`` context variable, set by the entry point that knows the client:

``MANUAL``
    A person: the operator console, MQTT commands, and ONVIF principals that are
    not autotrackers (for example a UniFi Protect user with the ``ptz`` role).
``AUTO``
    An autotracker or patrol: the ONVIF principals listed in
    ``ptz.autotrack_users`` (default ``frigate``), anonymous ONVIF callers, and
    the sentry patrol.
``None`` (untagged)
    Osprey itself (auto-home, recovery). It is never held back and never starts
    a hold.

A manual command starts or extends a hold of ``ptz.manual_hold_sec`` seconds
and supersedes an automatic move that is still in flight. While the hold lasts,
automatic commands are dropped. When the hold ends, autotracking resumes.
"""

from __future__ import annotations

import contextvars
import threading
import time
from typing import Callable, Final

MANUAL: Final = "manual"
AUTO: Final = "auto"

source: contextvars.ContextVar[str | None] = contextvars.ContextVar("ptz_source", default=None)

DEFAULT_HOLD_SEC: Final = 30.0


class Arbiter:
    """Per-camera manual hold. Thread-safe; ``hold_sec`` 0 turns arbitration off."""

    def __init__(self, hold_sec: float = DEFAULT_HOLD_SEC,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.hold_sec = max(0.0, float(hold_sec))
        self._clock = clock
        self._lock = threading.Lock()
        self._until: dict[str, float] = {}
        self._last: dict[str, str | None] = {}
        self.dropped = 0

    def admit(self, mac: str, kind: str | None) -> tuple[bool, bool]:
        """Return ``(allowed, supersede)`` for one command from ``kind``.

        ``supersede`` is true when a manual command may replace a move that an
        autotracker (or Osprey itself) started and that has not finished yet.
        """
        if self.hold_sec <= 0 or kind is None:
            return True, False
        now = self._clock()
        with self._lock:
            if kind == MANUAL:
                supersede = self._last.get(mac) != MANUAL
                self._until[mac] = now + self.hold_sec
                self._last[mac] = MANUAL
                return True, supersede
            if now < self._until.get(mac, 0.0):
                self.dropped += 1
                return False, False
            self._last[mac] = kind
            return True, False

    def remaining(self, mac: str) -> float:
        """Seconds left on the manual hold for ``mac`` (0 when none)."""
        with self._lock:
            return max(0.0, self._until.get(mac, 0.0) - self._clock())
