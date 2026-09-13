"""Save the physical camera's current position as home without moving it."""

from __future__ import annotations

import signal
import time
from contextlib import contextmanager
from types import FrameType
from typing import Callable, Iterator, TypeVar

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
    status = bounded(lambda: ptz.GetStatus({"ProfileToken": PROFILE}))
    move_status = getattr(status, "MoveStatus", None)
    states = (
        str(getattr(move_status, "PanTilt", "UNKNOWN")).upper(),
        str(getattr(move_status, "Zoom", "UNKNOWN")).upper(),
    )
    if states != ("IDLE", "IDLE"):
        raise RuntimeError(f"physical camera is not safely idle: {states}")

    # Preserve the existing token when replacing home.  Frigate caches preset
    # tokens at startup, so creating a second identically named preset would
    # leave its return-home action pointing at the old physical view.
    presets = bounded(lambda: ptz.GetPresets({"ProfileToken": PROFILE}))
    home = next(
        (
            preset
            for preset in presets
            if str(getattr(preset, "Name", "")).casefold() == PRESET
        ),
        None,
    )
    request: dict[str, str] = {"ProfileToken": PROFILE, "PresetName": PRESET}
    if home is not None:
        token = str(getattr(home, "token", getattr(home, "PresetToken", "")))
        if token:
            request["PresetToken"] = token
    token = bounded(
        lambda: ptz.SetPreset(request)
    )
    if token is None or str(token) == "":
        raise RuntimeError("Cuckoo returned no physical home preset token")
    print(f"stored current physical position as home preset token={token}", flush=True)


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
    print(f"failed to store physical home preset: {last}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
