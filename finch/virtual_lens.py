"""Command-driven virtual lens for a local MP4 fixture.

The one recorded field of view cannot contain scenery behind or above the real
camera.  To keep PTZ motion measurable without inventing pixels, the normalized
1280x720 image is repeated horizontally and reflected vertically.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final, Iterator

import gimbal as gimbal_module
import identity
from unifiwire import annexb, hevc


TEXTURE_WIDTH: Final = 1280
TEXTURE_HEIGHT: Final = 720
WIDE_HORIZONTAL_FOV: Final = 99.7
WIDE_VERTICAL_FOV: Final = 51.9
TELE_HORIZONTAL_FOV: Final = 45.5
TELE_VERTICAL_FOV: Final = 25.4
VIRTUAL_WORLD_WIDTH: Final = round(350.0 * TEXTURE_WIDTH / WIDE_HORIZONTAL_FOV)
VIRTUAL_WORLD_HEIGHT: Final = round(100.0 * TEXTURE_HEIGHT / WIDE_VERTICAL_FOV)
MAX_DIMENSION: Final = 8192
MAX_FRAME_BYTES: Final = 128 * 1024 * 1024
PROBE_TIMEOUT_SEC: Final = 10.0
STARTUP_TIMEOUT_SEC: Final = 15.0
PROCESS_STOP_TIMEOUT_SEC: Final = 3.0

log = logging.getLogger("finch.lens")


@dataclass(frozen=True)
class Viewport:
    x: int
    y: int
    width: int
    height: int


def _fraction(value: int, bounds: tuple[int, int]) -> float:
    low, high = bounds
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _degrees(value: int, steps: tuple[int, int], degrees: tuple[float, float]) -> float:
    return degrees[0] + _fraction(value, steps) * (degrees[1] - degrees[0])


def viewport(motor: dict[str, int]) -> Viewport:
    """Convert G5 motor coordinates to a crop in a repeated virtual world."""
    pan_degrees = _degrees(
        motor.get("pan", 18000), identity.PAN_STEPS, identity.PAN_DEGREES
    )
    tilt_degrees = _degrees(
        motor.get("tilt", 13000), identity.TILT_STEPS, identity.TILT_DEGREES
    )
    zoom = _fraction(
        motor.get("zoom", identity.ZOOM_STEPS[0]), identity.ZOOM_STEPS
    )
    horizontal_fov = WIDE_HORIZONTAL_FOV + zoom * (
        TELE_HORIZONTAL_FOV - WIDE_HORIZONTAL_FOV
    )
    vertical_fov = WIDE_VERTICAL_FOV + zoom * (
        TELE_VERTICAL_FOV - WIDE_VERTICAL_FOV
    )
    width = max(2, round(TEXTURE_WIDTH * horizontal_fov / WIDE_HORIZONTAL_FOV))
    height = max(2, round(TEXTURE_HEIGHT * vertical_fov / WIDE_VERTICAL_FOV))
    width -= width % 2
    height -= height % 2

    # Motor centre is 0 degrees pan, 40 degrees tilt.  Positive pan moves the
    # viewport right (image content left); positive tilt moves it down (content up).
    centre_x = round(pan_degrees * TEXTURE_WIDTH / WIDE_HORIZONTAL_FOV)
    centre_y = round((tilt_degrees - 40.0) * TEXTURE_HEIGHT / WIDE_VERTICAL_FOV)
    return Viewport(
        x=centre_x - width // 2,
        y=centre_y - height // 2,
        width=width,
        height=height,
    )


def _reflected(index: int, size: int) -> int:
    if size <= 1:
        return 0
    period = 2 * (size - 1)
    folded = index % period
    return folded if folded < size else period - folded


def crop_ppm(frame: bytes, crop: Viewport) -> bytes:
    """Sample a viewport from one RGB24 texture using wrap/reflection."""
    expected = TEXTURE_WIDTH * TEXTURE_HEIGHT * 3
    if expected > MAX_FRAME_BYTES or len(frame) != expected:
        raise ValueError(f"RGB frame is {len(frame)} bytes; expected {expected}")
    if min(crop.width, crop.height) <= 0 or max(crop.width, crop.height) > MAX_DIMENSION:
        raise ValueError("invalid viewport dimensions")

    stride = TEXTURE_WIDTH * 3
    row_bytes = crop.width * 3
    body = bytearray(row_bytes * crop.height)
    for row in range(crop.height):
        source_y = _reflected(
            crop.y + row + TEXTURE_HEIGHT // 2, TEXTURE_HEIGHT
        )
        source_row = source_y * stride
        target = row * row_bytes
        remaining = crop.width
        source_x = (crop.x + TEXTURE_WIDTH // 2) % TEXTURE_WIDTH
        while remaining:
            run = min(remaining, TEXTURE_WIDTH - source_x)
            begin = source_row + source_x * 3
            count = run * 3
            body[target : target + count] = frame[begin : begin + count]
            target += count
            remaining -= run
            source_x = 0
    return f"P6\n{crop.width} {crop.height}\n255\n".encode("ascii") + body


def probe_dimensions(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=PROBE_TIMEOUT_SEC,
    )
    values = result.stdout.strip().split("x")
    if len(values) != 2 or not all(value.isdigit() for value in values):
        raise ValueError(f"ffprobe returned invalid dimensions: {result.stdout!r}")
    width, height = int(values[0]), int(values[1])
    if min(width, height) <= 0 or max(width, height) > MAX_DIMENSION:
        raise ValueError(f"unsupported source dimensions {width}x{height}")
    return width, height


def encoder_command(fps: int, width: int, height: int) -> list[str]:
    if fps <= 0 or fps > 120:
        raise ValueError("fps must be between 1 and 120")
    if min(width, height) <= 0 or max(width, height) > MAX_DIMENSION:
        raise ValueError("invalid output dimensions")
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "image2pipe", "-framerate", str(fps), "-vcodec", "ppm",
        "-i", "pipe:0", "-an", "-vf",
        f"scale={width}:{height}:flags=fast_bilinear,format=yuv420p",
        "-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency",
        "-x265-params",
        f"keyint={fps}:min-keyint={fps}:scenecut=0:repeat-headers=1:log-level=error",
        "-f", "hevc", "pipe:1",
    ]


def _read_exact(stream: BinaryIO, size: int, stopping: threading.Event) -> bytes | None:
    data = bytearray()
    while len(data) < size and not stopping.is_set():
        chunk = stream.read(size - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data) if len(data) == size else None


def _annex_b_units(stream: BinaryIO, stopping: threading.Event) -> Iterator[bytes]:
    buffer = bytearray()
    marker = annexb.START_CODE
    while not stopping.is_set():
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            first = buffer.find(marker)
            if first < 0:
                if len(buffer) > 2:
                    del buffer[:-2]
                break
            if first:
                del buffer[:first]
            following = buffer.find(marker, len(marker))
            if following < 0:
                break
            unit = bytes(buffer[len(marker) : following]).rstrip(b"\x00")
            del buffer[:following]
            if unit:
                yield unit
    first = buffer.find(marker)
    if first >= 0:
        unit = bytes(buffer[first + len(marker) :]).rstrip(b"\x00")
        if unit:
            yield unit


@dataclass
class LensSource:
    path: Path
    fps: int
    output_width: int
    output_height: int
    head: gimbal_module.Gimbal
    state_path: Path | None = None
    _stopping: threading.Event = field(default_factory=threading.Event, init=False)
    _decoder: subprocess.Popen[bytes] | None = field(default=None, init=False)
    _encoder: subprocess.Popen[bytes] | None = field(default=None, init=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False)
    _units: queue.Queue[bytes | None] = field(
        default_factory=lambda: queue.Queue(maxsize=8), init=False
    )
    _pending: list[bytes] = field(default_factory=list, init=False)
    _last_viewport: Viewport | None = field(default=None, init=False)
    _close_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        encoder_command(self.fps, self.output_width, self.output_height)

    def _start(self) -> None:
        with self._close_lock:
            self._close_unlocked()
            source_width, source_height = probe_dimensions(self.path)
            log.info("normalizing local source %dx%d to %dx%d", source_width, source_height, TEXTURE_WIDTH, TEXTURE_HEIGHT)
            self._stopping = threading.Event()
            self._units = queue.Queue(maxsize=8)
            self._pending = []
            self._last_viewport = None
            encoder = subprocess.Popen(
                encoder_command(self.fps, self.output_width, self.output_height),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
            try:
                decoder = subprocess.Popen(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
                        "-stream_loop", "-1", "-re", "-i", str(self.path), "-map", "0:v:0",
                        "-an", "-vf", f"fps={self.fps},scale={TEXTURE_WIDTH}:{TEXTURE_HEIGHT}:flags=fast_bilinear",
                        "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                )
            except Exception:
                self._stop_process(encoder)
                raise
            self._encoder, self._decoder = encoder, decoder
            assert encoder.stdin is not None and encoder.stdout is not None and encoder.stderr is not None
            assert decoder.stdout is not None and decoder.stderr is not None
            self._threads = [
                threading.Thread(target=self._feed, args=(decoder.stdout, encoder.stdin), daemon=True),
                threading.Thread(target=self._read_units, args=(encoder.stdout,), daemon=True),
                threading.Thread(target=self._drain, args=("decoder", decoder.stderr), daemon=True),
                threading.Thread(target=self._drain, args=("encoder", encoder.stderr), daemon=True),
            ]
            for thread in self._threads:
                thread.start()
        log.info("virtual lens ready: %dx%d HEVC at %dfps", self.output_width, self.output_height, self.fps)

    def _feed(self, decoded: BinaryIO, encoded: BinaryIO) -> None:
        frame_size = TEXTURE_WIDTH * TEXTURE_HEIGHT * 3
        try:
            while not self._stopping.is_set():
                frame = _read_exact(decoded, frame_size, self._stopping)
                if frame is None:
                    return
                motor = self.head.snapshot()
                crop = viewport(motor)
                if crop != self._last_viewport:
                    self._last_viewport = crop
                    self._publish(crop, motor)
                    log.info("viewport x=%d y=%d width=%d height=%d", crop.x, crop.y, crop.width, crop.height)
                encoded.write(crop_ppm(frame, crop))
                encoded.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            if not self._stopping.is_set():
                log.warning("virtual lens feeder stopped: %s", exc)
        finally:
            try:
                encoded.close()
            except OSError:
                pass

    def _publish(self, crop: Viewport, motor: dict[str, int]) -> None:
        if self.state_path is None:
            return
        payload = {
            "texture": {"width": TEXTURE_WIDTH, "height": TEXTURE_HEIGHT, "mode": "horizontal-wrap-vertical-reflect"},
            "virtual_world": {"width": VIRTUAL_WORLD_WIDTH, "height": VIRTUAL_WORLD_HEIGHT},
            "output": {"width": self.output_width, "height": self.output_height},
            "viewport": {"x": crop.x, "y": crop.y, "width": crop.width, "height": crop.height},
            "motor": {axis: motor[axis] for axis in ("pan", "tilt", "zoom")},
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _read_units(self, encoded: BinaryIO) -> None:
        try:
            for unit in _annex_b_units(encoded, self._stopping):
                while not self._stopping.is_set():
                    try:
                        self._units.put(unit, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        finally:
            while not self._stopping.is_set():
                try:
                    self._units.put(None, timeout=0.2)
                    break
                except queue.Full:
                    continue

    def _drain(self, name: str, stream: BinaryIO) -> None:
        for raw in iter(stream.readline, b""):
            if self._stopping.is_set():
                return
            log.debug("ffmpeg %s: %s", name, raw.decode("utf-8", "replace").rstrip()[:1000])

    def _next_unit(self, timeout: float) -> bytes | None:
        try:
            return self._units.get(timeout=timeout)
        except queue.Empty:
            if self._stopping.is_set():
                return None
            if self._encoder is not None and self._encoder.poll() is not None:
                raise OSError(f"virtual-lens encoder exited with {self._encoder.returncode}")
            raise TimeoutError("timed out waiting for virtual-lens HEVC")

    def hvcc(self) -> bytes:
        self._start()
        found: dict[int, bytes] = {}
        deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
        while len(found) < 3:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise TimeoutError("virtual-lens encoder did not produce parameter sets")
            try:
                unit = self._next_unit(min(1.0, remaining))
            except TimeoutError:
                continue
            if unit is None:
                self.close()
                raise OSError("virtual-lens encoder stopped during startup")
            kind = hevc.nal_type(unit)
            if kind in (hevc.NAL_VPS, hevc.NAL_SPS, hevc.NAL_PPS):
                found[kind] = unit
            else:
                self._pending.append(unit)
        return annexb.build_hvcc(found[hevc.NAL_VPS], found[hevc.NAL_SPS], found[hevc.NAL_PPS])

    def pictures(self) -> Iterator[tuple[list[bytes], bool]]:
        pending, self._pending = self._pending, []
        while not self._stopping.is_set():
            try:
                unit = self._next_unit(1.0)
            except TimeoutError:
                continue
            if unit is None:
                return
            kind = hevc.nal_type(unit)
            if kind in (hevc.NAL_VPS, hevc.NAL_SPS, hevc.NAL_PPS):
                continue
            if kind is not None and kind <= annexb.VCL_MAX:
                yield pending + [unit], hevc.is_irap(unit)
                pending = []
            else:
                pending.append(unit)

    def close(self) -> None:
        with self._close_lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        self._stopping.set()
        for process in (self._decoder, self._encoder):
            if process is not None:
                self._stop_process(process)
        for thread in self._threads:
            thread.join(timeout=PROCESS_STOP_TIMEOUT_SEC)
        self._threads = []
        self._decoder = self._encoder = None

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SEC)
