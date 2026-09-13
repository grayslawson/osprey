from __future__ import annotations

import signal
import sys
import time
from contextlib import contextmanager
from types import FrameType
from typing import Any, Callable, Iterator, TypeVar

from onvif import ONVIFCamera


HOST = "cuckoo"
PORT = 8000
PROFILE = "video2"
PRESET = "home"
T = TypeVar("T")


def _timeout(signum: int, frame: FrameType | None) -> None:
    raise TimeoutError("ONVIF operation timed out")


@contextmanager
def deadline(seconds: float) -> Iterator[None]:
    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def bounded(call: Callable[[], T]) -> T:
    with deadline(8):
        return call()


def ensure_home() -> None:
    camera = bounded(lambda: ONVIFCamera(HOST, PORT, "", ""))
    ptz = camera.create_ptz_service()
    bounded(
        lambda: ptz.AbsoluteMove(
            {
                "ProfileToken": PROFILE,
                "Position": {
                    "PanTilt": {"x": 0.0, "y": 0.0},
                    "Zoom": {"x": 0.0},
                },
            }
        )
    )

    move_deadline = time.monotonic() + 20
    last_states = ("UNKNOWN", "UNKNOWN")
    while time.monotonic() < move_deadline:
        status = bounded(lambda: ptz.GetStatus({"ProfileToken": PROFILE}))
        move_status = getattr(status, "MoveStatus", None)
        last_states = (
            str(getattr(move_status, "PanTilt", "UNKNOWN")).upper(),
            str(getattr(move_status, "Zoom", "UNKNOWN")).upper(),
        )
        if last_states == ("IDLE", "IDLE"):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(
            "synthetic camera did not become IDLE at center; "
            f"last move status={last_states}"
        )

    token: Any = bounded(
        lambda: ptz.SetPreset(
            {
                "ProfileToken": PROFILE,
                "PresetName": PRESET,
            }
        )
    )
    if token is None or str(token) == "":
        raise RuntimeError("Cuckoo returned no token for synthetic home preset")
    print(
        f"centered synthetic camera and stored home preset token={token}",
        flush=True,
    )


def main() -> int:
    stop = time.monotonic() + 45
    last: BaseException | None = None
    while time.monotonic() < stop:
        try:
            ensure_home()
            return 0
        except (OSError, RuntimeError, TimeoutError) as exc:
            last = exc
            time.sleep(1)
    print(f"failed to prepare Frigate home preset: {last}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
