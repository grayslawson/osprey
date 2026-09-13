"""Prove commanded PTZ changes the frames Frigate actually receives."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Iterator, TypeVar

os.environ.setdefault("XDG_CACHE_HOME", "/tmp/cache")

from onvif import ONVIFCamera


BASE_URL = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
CAMERA = os.environ.get("FRIGATE_CAMERA", "synthetic_g5_ptz")
TIMEOUT = float(os.environ.get("FRIGATE_VISUAL_TIMEOUT", "120"))
STATE_PATH = Path("/finch-state/finch-lens.json")
ONVIF_HOST = os.environ.get("ONVIF_HOST", "cuckoo")
ONVIF_PORT = int(os.environ.get("ONVIF_PORT", "8000"))
PROFILE = "video2"
FRAME_WIDTH = 160
FRAME_HEIGHT = 90
PAN_FOV_SPACE = (
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"
)
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


def api(path: str, method: str = "GET", payload: dict[str, str] | None = None) -> bytes:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.read()


def set_autotracker(enabled: bool) -> None:
    camera = urllib.parse.quote(CAMERA, safe="")
    api(
        f"/api/camera/{camera}/set/ptz_autotracker",
        method="PUT",
        payload={"value": "ON" if enabled else "OFF"},
    )


def lens_state() -> dict[str, Any]:
    value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("virtual-lens state is not an object")
    return value


def axis_state(status: Any, axis: str) -> str:
    move_status = getattr(status, "MoveStatus", None)
    return str(getattr(move_status, axis, "UNKNOWN")).upper()


def wait_idle(ptz: Any, stop: float) -> None:
    last = ("UNKNOWN", "UNKNOWN")
    while time.monotonic() < stop:
        status = bounded(lambda: ptz.GetStatus({"ProfileToken": PROFILE}))
        last = (axis_state(status, "PanTilt"), axis_state(status, "Zoom"))
        if last == ("IDLE", "IDLE"):
            return
        time.sleep(0.1)
    raise TimeoutError(f"PTZ did not become IDLE; last status={last}")


def wait_state_change(before: dict[str, Any], stop: float) -> dict[str, Any]:
    while time.monotonic() < stop:
        current = lens_state()
        if current.get("motor") != before.get("motor"):
            return current
        time.sleep(0.05)
    raise TimeoutError("virtual-lens motor/viewport state did not change")


def frame() -> list[int]:
    camera = urllib.parse.quote(CAMERA, safe="")
    jpeg = api(f"/api/{camera}/latest.jpg?h=720&_={time.time_ns()}")
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "image2pipe",
            "-i",
            "pipe:0",
            "-vf",
            f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=fast_bilinear,format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        input=jpeg,
        capture_output=True,
        check=True,
        timeout=8,
    )
    expected = FRAME_WIDTH * FRAME_HEIGHT
    if len(result.stdout) != expected:
        raise ValueError(f"decoded frame has {len(result.stdout)} bytes, expected {expected}")
    return list(result.stdout)


def contrast(values: list[int]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def wait_textured_frame(stop: float) -> list[int]:
    last_contrast = 0.0
    while time.monotonic() < stop:
        candidate = frame()
        last_contrast = contrast(candidate)
        if last_contrast >= 12.0:
            return candidate
        time.sleep(0.25)
    raise TimeoutError(f"no textured Frigate frame arrived; last contrast={last_contrast:.2f}")


def correlation(pairs: Iterator[tuple[int, int]]) -> float:
    count = 0
    sum_a = sum_b = sum_aa = sum_bb = sum_ab = 0.0
    for a, b in pairs:
        count += 1
        sum_a += a
        sum_b += b
        sum_aa += a * a
        sum_bb += b * b
        sum_ab += a * b
    if count < 100:
        return -1.0
    covariance = sum_ab - sum_a * sum_b / count
    variance_a = sum_aa - sum_a * sum_a / count
    variance_b = sum_bb - sum_b * sum_b / count
    denominator = math.sqrt(max(variance_a * variance_b, 0.0))
    return covariance / denominator if denominator else -1.0


def translated_correlation(before: list[int], after: list[int], dx: int, dy: int) -> float:
    margin_x = 24
    margin_y = 12
    return correlation(
        (
            before[(y + dy) * FRAME_WIDTH + x + dx],
            after[y * FRAME_WIDTH + x],
        )
        for y in range(margin_y, FRAME_HEIGHT - margin_y)
        for x in range(margin_x, FRAME_WIDTH - margin_x)
        if 0 <= x + dx < FRAME_WIDTH and 0 <= y + dy < FRAME_HEIGHT
    )


def best_translation(before: list[int], after: list[int]) -> tuple[int, int, float]:
    candidates = (
        (dx, dy, translated_correlation(before, after, dx, dy))
        for dy in range(-10, 11, 2)
        for dx in range(-30, 31, 2)
    )
    return max(candidates, key=lambda candidate: candidate[2])


def scaled_correlation(
    before: list[int], after: list[int], scale: float, dx: int, dy: int
) -> float:
    centre_x = (FRAME_WIDTH - 1) / 2
    centre_y = (FRAME_HEIGHT - 1) / 2

    def pairs() -> Iterator[tuple[int, int]]:
        for y in range(12, FRAME_HEIGHT - 12):
            source_y = round(centre_y + (y - centre_y) * scale + dy)
            if not 0 <= source_y < FRAME_HEIGHT:
                continue
            for x in range(24, FRAME_WIDTH - 24):
                source_x = round(centre_x + (x - centre_x) * scale + dx)
                if 0 <= source_x < FRAME_WIDTH:
                    yield (
                        before[source_y * FRAME_WIDTH + source_x],
                        after[y * FRAME_WIDTH + x],
                    )

    return correlation(pairs())


def best_scale(before: list[int], after: list[int]) -> tuple[float, int, int, float]:
    candidates = (
        (scale / 100, dx, dy, scaled_correlation(before, after, scale / 100, dx, dy))
        for scale in range(55, 126, 5)
        for dy in range(-6, 7, 3)
        for dx in range(-8, 9, 4)
    )
    return max(candidates, key=lambda candidate: candidate[3])


def best_translation_observation(
    before: list[int], stop: float
) -> tuple[int, int, float]:
    best = (0, 0, -1.0)
    while time.monotonic() < stop:
        candidate = frame()
        if translated_correlation(before, candidate, 0, 0) < 0.995:
            best = max(
                best,
                best_translation(before, candidate),
                key=lambda value: value[2],
            )
        time.sleep(0.2)
    return best


def best_zoom_observation(
    before: list[int], stop: float
) -> tuple[float, int, int, float]:
    best = (1.0, 0, 0, -1.0)
    while time.monotonic() < stop:
        candidate = frame()
        if translated_correlation(before, candidate, 0, 0) < 0.995:
            best = max(
                best,
                best_scale(before, candidate),
                key=lambda value: value[3],
            )
        time.sleep(0.2)
    return best


def viewport_centre_x(state: dict[str, Any]) -> float:
    viewport = state["viewport"]
    return float(viewport["x"]) + float(viewport["width"]) / 2


def viewport_centre_y(state: dict[str, Any]) -> float:
    viewport = state["viewport"]
    return float(viewport["y"]) + float(viewport["height"]) / 2


def main() -> int:
    stop = time.monotonic() + TIMEOUT
    if not STATE_PATH.is_file():
        raise SystemExit(
            "virtual-lens state is absent; recreate Finch with FINCH_VIRTUAL_LENS=1"
        )

    camera = bounded(lambda: ONVIFCamera(ONVIF_HOST, ONVIF_PORT, "", ""))
    ptz = camera.create_ptz_service()
    presets = bounded(lambda: ptz.GetPresets({"ProfileToken": PROFILE}))
    home = next(
        (preset for preset in presets if str(getattr(preset, "Name", "")) == "home"),
        None,
    )
    if home is None:
        raise SystemExit("synthetic camera has no home preset; run frigate-init first")
    home_token = str(getattr(home, "token", getattr(home, "PresetToken", "")))

    set_autotracker(False)
    try:
        bounded(
            lambda: ptz.GotoPreset(
                {"ProfileToken": PROFILE, "PresetToken": home_token}
            )
        )
        wait_idle(ptz, stop)
        time.sleep(1.0)
        before_pan_state = lens_state()
        before_pan_frame = wait_textured_frame(stop)

        bounded(
            lambda: ptz.RelativeMove(
                {
                    "ProfileToken": PROFILE,
                    "Translation": {
                        "PanTilt": {"x": 0.25, "y": 0.0, "space": PAN_FOV_SPACE},
                        "Zoom": {"x": 0.0},
                    },
                }
            )
        )
        wait_state_change(before_pan_state, stop)
        wait_idle(ptz, stop)
        after_pan_state = lens_state()
        # Sample immediately after motion settles. The private G5 clip contains
        # real camera movement, so long delays let source motion overwhelm the
        # commanded virtual-lens transform.
        time.sleep(0.25)
        pan_observation = best_translation_observation(
            before_pan_frame, min(stop, time.monotonic() + 3)
        )

        pan_motor_delta = float(after_pan_state["motor"]["pan"]) - float(
            before_pan_state["motor"]["pan"]
        )
        pan_view_delta = viewport_centre_x(after_pan_state) - viewport_centre_x(
            before_pan_state
        )
        frame_shift_x, frame_shift_y, pan_correlation = pan_observation
        if pan_motor_delta <= 0 or pan_view_delta <= 0:
            raise AssertionError(
                "positive FOV pan did not move motor and viewport right: "
                f"motor_delta={pan_motor_delta}, viewport_delta={pan_view_delta}"
            )
        if frame_shift_x <= 0 or pan_correlation < 0.35:
            raise AssertionError(
                "Frigate frames did not show the commanded rightward viewport "
                f"(leftward content) shift: dx={frame_shift_x}, dy={frame_shift_y}, "
                f"correlation={pan_correlation:.3f}, motor_delta={pan_motor_delta}, "
                f"viewport_delta={pan_view_delta}"
            )

        before_tilt_state = after_pan_state
        before_tilt_frame = wait_textured_frame(stop)
        bounded(
            lambda: ptz.RelativeMove(
                {
                    "ProfileToken": PROFILE,
                    "Translation": {
                        "PanTilt": {
                            "x": 0.0,
                            "y": 0.25,
                            "space": PAN_FOV_SPACE,
                        },
                        "Zoom": {"x": 0.0},
                    },
                }
            )
        )
        wait_state_change(before_tilt_state, stop)
        wait_idle(ptz, stop)
        after_tilt_state = lens_state()
        time.sleep(0.25)
        tilt_observation = best_translation_observation(
            before_tilt_frame, min(stop, time.monotonic() + 3)
        )
        tilt_motor_delta = float(after_tilt_state["motor"]["tilt"]) - float(
            before_tilt_state["motor"]["tilt"]
        )
        tilt_view_delta = viewport_centre_y(after_tilt_state) - viewport_centre_y(
            before_tilt_state
        )
        tilt_shift_x, tilt_shift_y, tilt_correlation = tilt_observation
        if tilt_motor_delta >= 0 or tilt_view_delta >= 0:
            raise AssertionError(
                "positive ONVIF tilt did not move motor/viewport upward: "
                f"motor_delta={tilt_motor_delta}, viewport_delta={tilt_view_delta}"
            )
        if tilt_shift_y >= 0 or tilt_correlation < 0.35:
            raise AssertionError(
                "Frigate frames did not show commanded upward viewport "
                f"(downward content) shift: dx={tilt_shift_x}, "
                f"dy={tilt_shift_y}, correlation={tilt_correlation:.3f}, "
                f"motor_delta={tilt_motor_delta}, viewport_delta={tilt_view_delta}"
            )

        before_zoom_state = after_tilt_state
        before_zoom_frame = wait_textured_frame(stop)
        bounded(
            lambda: ptz.RelativeMove(
                {
                    "ProfileToken": PROFILE,
                    "Translation": {
                        "PanTilt": {"x": 0.0, "y": 0.0, "space": PAN_FOV_SPACE},
                        "Zoom": {"x": 0.35},
                    },
                }
            )
        )
        wait_state_change(before_zoom_state, stop)
        wait_idle(ptz, stop)
        after_zoom_state = lens_state()
        time.sleep(0.25)
        zoom_observation = best_zoom_observation(
            before_zoom_frame, min(stop, time.monotonic() + 3)
        )

        before_width = float(before_zoom_state["viewport"]["width"])
        after_width = float(after_zoom_state["viewport"]["width"])
        zoom_motor_delta = float(after_zoom_state["motor"]["zoom"]) - float(
            before_zoom_state["motor"]["zoom"]
        )
        expected_scale = after_width / before_width
        observed_scale, zoom_dx, zoom_dy, zoom_correlation = zoom_observation
        if zoom_motor_delta <= 0 or expected_scale >= 1.0:
            raise AssertionError(
                "positive zoom did not increase motor zoom and narrow viewport: "
                f"motor_delta={zoom_motor_delta}, width_ratio={expected_scale:.3f}"
            )
        if observed_scale >= 0.98 or abs(observed_scale - expected_scale) > 0.20:
            raise AssertionError(
                "Frigate frames did not magnify in the commanded zoom direction: "
                f"observed_scale={observed_scale:.3f}, "
                f"viewport_scale={expected_scale:.3f}, "
                f"correlation={zoom_correlation:.3f}"
            )
        if zoom_correlation < 0.30:
            raise AssertionError(
                f"zoomed Frigate frame correlation is too weak: {zoom_correlation:.3f}"
            )

        print(
            json.dumps(
                {
                    "camera": CAMERA,
                    "pan": {
                        "motor_delta": pan_motor_delta,
                        "viewport_centre_x_delta": pan_view_delta,
                        "frame_content_shift_x": frame_shift_x,
                        "frame_content_shift_y": frame_shift_y,
                        "correlation": round(pan_correlation, 4),
                    },
                    "tilt": {
                        "motor_delta": tilt_motor_delta,
                        "viewport_centre_y_delta": tilt_view_delta,
                        "frame_content_shift_x": tilt_shift_x,
                        "frame_content_shift_y": tilt_shift_y,
                        "correlation": round(tilt_correlation, 4),
                    },
                    "zoom": {
                        "motor_delta": zoom_motor_delta,
                        "viewport_width_ratio": round(expected_scale, 4),
                        "frame_observed_scale": observed_scale,
                        "frame_alignment_x": zoom_dx,
                        "frame_alignment_y": zoom_dy,
                        "correlation": round(zoom_correlation, 4),
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        try:
            bounded(
                lambda: ptz.GotoPreset(
                    {"ProfileToken": PROFILE, "PresetToken": home_token}
                )
            )
            wait_idle(ptz, time.monotonic() + 25)
        finally:
            set_autotracker(True)


if __name__ == "__main__":
    raise SystemExit(main())
