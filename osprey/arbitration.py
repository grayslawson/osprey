"""PTZ arbitration between people and autotrackers: manual wins, then resume.

Every PTZ command is tagged with the kind of client that sent it, through the
``source`` context variable, set by the entry point that knows the client:

``MANUAL``
    A person: the operator console, MQTT commands, and ONVIF principals that are
    not autotrackers (for example a UniFi Protect user with the ``ptz`` role).
``AUTO``
    An autotracker: the ONVIF principals listed in ``ptz.autotrack_users``
    (default ``frigate``) and anonymous ONVIF callers.
``PATROL``
    The sentry patrol. It behaves like an autotracker for arbitration — a
    manual hold stops it — but it is a separate kind so the controller can
    tell an autotracker apart from a patrol. The patrol needs that
    distinction because it has to stand down while Frigate is tracking, and a
    patrol that mistook its own moves for tracking would never advance. The
    priority runs the other way too: an autotracker takes the camera from a
    patrol that is still travelling to its next point, because a tracked
    object outranks idle-time work.
``ASSIST``
    Osprey Assist, the optional image-quality sidecar. It ranks exactly like
    the patrol: a manual hold stops it, an autotracker takes the camera from
    it, it never starts a hold and it does not count as autotracking. It has
    its own kind so logs and counters can tell its moves apart.
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
PATROL: Final = "patrol"
ASSIST: Final = "assist"

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

        ``supersede`` is true when the command may replace a move that another
        source started and that has not finished yet:

        * a person may replace a move an autotracker, a patrol or Osprey
          itself started;
        * an autotracker may replace a move the sentry patrol or Assist
          started.  Both are idle-time work and a tracked object outranks them.  Without
          this the autotracker's first move is refused as "the camera is
          already moving" for as long as the head is travelling to the next
          patrol point, and the tracking never starts.
        """
        if kind is None:
            return True, False
        now = self._clock()
        with self._lock:
            if kind == MANUAL:
                if self.hold_sec <= 0:
                    # Arbitration is off: nobody supersedes anybody.
                    return True, False
                supersede = self._last.get(mac) != MANUAL
                self._until[mac] = now + self.hold_sec
                self._last[mac] = MANUAL
                return True, supersede
            if self.hold_sec > 0 and now < self._until.get(mac, 0.0):
                self.dropped += 1
                return False, False
            supersede = kind == AUTO and self._last.get(mac) in (PATROL, ASSIST)
            self._last[mac] = kind
            return True, supersede

    def remaining(self, mac: str) -> float:
        """Seconds left on the manual hold for ``mac`` (0 when none)."""
        with self._lock:
            return max(0.0, self._until.get(mac, 0.0) - self._clock())
