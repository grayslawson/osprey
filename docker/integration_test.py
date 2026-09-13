from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Iterator, TypeVar

from onvif import ONVIFCamera


HOST = os.environ.get("ONVIF_HOST", "cuckoo")
PORT = int(os.environ.get("ONVIF_PORT", "8000"))
TRACK = os.environ.get("RTSP_TRACK", "video2")
FOV_TRANSLATION_SPACE = (
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"
)
EXPECTED_MAC = os.environ.get("EXPECTED_MAC", "02C0FFEE1201").upper()
EXPECTED_MODELS = {
    value.strip()
    for value in os.environ.get("EXPECTED_MODELS", "0xa59b,UVC G5 PTZ").split(",")
    if value.strip()
}
CUCKOO_TRAFFIC = Path("/cuckoo-state/cuckoo-messages.jsonl")
FINCH_TRAFFIC = Path("/finch-state/finch-messages.jsonl")

T = TypeVar("T")


class CheckFailure(RuntimeError):
    pass


def require(ok: bool, name: str, detail: str = "") -> None:
    if not ok:
        raise CheckFailure(f"{name}: {detail}".rstrip())
    print(f"[PASS] {name}" + (f" - {detail}" if detail else ""), flush=True)


def _timeout(signum: int, frame: FrameType | None) -> None:
    raise TimeoutError("operation timed out")


@contextmanager
def deadline(seconds: float) -> Iterator[None]:
    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def bounded(call: Callable[[], T], seconds: float = 8.0) -> T:
    with deadline(seconds):
        return call()


def eventually(call: Callable[[], T], seconds: float, label: str) -> T:
    stop = time.monotonic() + seconds
    last: BaseException | None = None
    while time.monotonic() < stop:
        try:
            return call()
        except (CheckFailure, OSError, TimeoutError, RuntimeError) as exc:
            last = exc
            time.sleep(1)
    raise CheckFailure(f"{label}: {last}")


def messages(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    found: list[dict[str, Any]] = []
    for raw in path.read_text().splitlines():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def count_traffic(name: str, direction: str | None = None) -> int:
    return sum(
        1
        for message in messages(CUCKOO_TRAFFIC)
        if message.get("functionName") == name
        and (direction is None or message.get("direction") == direction)
    )


def connect_onvif() -> ONVIFCamera:
    camera = bounded(lambda: ONVIFCamera(HOST, PORT, "", ""), 10)
    info = bounded(camera.devicemgmt.GetDeviceInformation)
    serial = str(info.SerialNumber).upper()
    model = str(info.Model)
    if serial != EXPECTED_MAC or model not in EXPECTED_MODELS:
        raise CheckFailure(f"unexpected ONVIF identity: serial={serial}, model={model}")
    require(True, "synthetic MAC adopted", serial)
    require(True, "synthetic G5 PTZ model", model)
    return camera


def probe(uri: str) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,r_frame_rate,avg_frame_rate",
        "-of", "json",
        uri,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    if completed.returncode != 0:
        raise CheckFailure(f"ffprobe failed ({completed.returncode}): {completed.stderr.strip()[-500:]}")
    payload = json.loads(completed.stdout)
    streams = payload.get("streams", [])
    if not streams:
        raise CheckFailure("ffprobe returned no video stream")
    stream = streams[0]
    if not isinstance(stream, dict):
        raise CheckFailure("ffprobe returned a malformed stream")
    return stream


def movement_state(status: Any) -> str:
    return str(status.MoveStatus.PanTilt).upper()


def position(status: Any) -> tuple[float, float]:
    point = status.Position.PanTilt
    return float(point.x), float(point.y)


def relative_pan_tilt_spaces(options: Any) -> list[str]:
    spaces = getattr(getattr(options, "Spaces", None), "RelativePanTiltTranslationSpace", [])
    return [str(getattr(space, "URI", "")) for space in spaces or []]


def capability_attribute(capabilities: Any, name: str) -> Any:
    """Read standard or extension attributes from different ONVIF WSDL vintages."""
    direct = getattr(capabilities, name, None)
    if direct is not None:
        return direct
    extension = getattr(capabilities, "_attr_1", None)
    return extension.get(name) if isinstance(extension, dict) else None


def main() -> int:
    address = socket.gethostbyname(HOST)
    require(address.startswith(("10.", "172.", "192.168.")), "private Compose DNS", f"{HOST}={address}")
    with socket.create_connection((HOST, PORT), timeout=2.0):
        pass
    require(True, "Cuckoo health endpoint", "ONVIF listener reachable")

    camera = eventually(connect_onvif, 45, "ONVIF camera did not become ready")
    media = camera.create_media_service()
    ptz = camera.create_ptz_service()

    capabilities = bounded(ptz.GetServiceCapabilities)
    move_status = capability_attribute(capabilities, "MoveStatus")
    require(
        str(move_status).lower() == "true",
        "ONVIF PTZ MoveStatus capability",
        str(move_status),
    )
    options = bounded(
        lambda: ptz.GetConfigurationOptions({"ConfigurationToken": "PTZConfig"})
    )
    relative_spaces = relative_pan_tilt_spaces(options)
    require(
        FOV_TRANSLATION_SPACE in relative_spaces,
        "ONVIF FOV-relative translation space",
        repr(relative_spaces),
    )

    profiles = bounded(media.GetProfiles)
    profile = next((item for item in profiles if str(item.token) == TRACK), None)
    require(profile is not None, "ONVIF media profile", f"track={TRACK}, profiles={[str(p.token) for p in profiles]}")
    token = str(profile.token)
    uri = str(bounded(lambda: media.GetStreamUri({
        "StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
        "ProfileToken": token,
    })).Uri)
    require(uri == f"rtsp://{HOST}:8554/{TRACK}", "ONVIF RTSP URI", uri)

    stream = eventually(lambda: probe(uri), 60, "RTSP stream did not become probeable")
    fps = str(stream.get("r_frame_rate") or stream.get("avg_frame_rate") or "")
    require(stream.get("codec_name") == "hevc", "RTSP codec", str(stream.get("codec_name")))
    require(stream.get("width") == 1280 and stream.get("height") == 720, "RTSP dimensions", f"{stream.get('width')}x{stream.get('height')}")
    fps_value = float(Fraction(fps))
    require(14.5 <= fps_value <= 15.5, "RTSP frame rate", f"{fps} ({fps_value:.3f} FPS)")

    initial = bounded(lambda: ptz.GetStatus({"ProfileToken": token}))
    start = position(initial)
    target = (-0.60 if start[0] >= 0 else 0.60, 0.30 if start[1] <= 0 else -0.30)
    preset_before = count_traffic("Preset", "out")
    motor_before = count_traffic("EventMotorState", "in")

    bounded(lambda: ptz.AbsoluteMove({
        "ProfileToken": token,
        "Position": {"PanTilt": {"x": target[0], "y": target[1]}},
    }))

    moving_seen = False
    final_status: Any | None = None
    stop = time.monotonic() + 30
    while time.monotonic() < stop:
        status = bounded(lambda: ptz.GetStatus({"ProfileToken": token}))
        state = movement_state(status)
        moving_seen = moving_seen or state == "MOVING"
        if moving_seen and state == "IDLE":
            final_status = status
            break
        time.sleep(0.1)

    require(moving_seen, "PTZ reports MOVING")
    require(final_status is not None, "PTZ returns to IDLE")
    final = position(final_status)
    require(abs(final[0] - target[0]) <= 0.08 and abs(final[1] - target[1]) <= 0.08,
            "final virtual position", f"requested={target}, final={final}")

    eventually(
        lambda: require(count_traffic("Preset", "out") >= preset_before + 2,
                        "PTZ preset commands reached Finch",
                        f"before={preset_before}, after={count_traffic('Preset', 'out')}") or True,
        10,
        "PTZ preset traffic missing",
    )
    eventually(
        lambda: require(count_traffic("EventMotorState", "in") > motor_before,
                        "Finch motor-state updates reached Cuckoo",
                        f"before={motor_before}, after={count_traffic('EventMotorState', 'in')}") or True,
        10,
        "motor-state traffic missing",
    )

    finch_names = {str(item.get("functionName", "")) for item in messages(FINCH_TRAFFIC)}
    require({"ChangeVideoSettings", "EnablePtzControl"} <= finch_names,
            "Finch adoption and PTZ channel configured",
            "ChangeVideoSettings + EnablePtzControl observed")

    print("[PASS] integration smoke test complete", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckFailure, OSError, TimeoutError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
