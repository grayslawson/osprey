"""The local MP4 renderer follows the simulated motors, not a canned pan."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from pathlib import Path

import gimbal
import pytest
import virtual_lens
from unifiwire import annexb, hevc


CENTRE = {"pan": 18000, "tilt": 13000, "zoom": 0, "focus": 58}


def test_motor_position_drives_physically_scaled_viewport() -> None:
    centred = virtual_lens.viewport(CENTRE)
    quarter_relative = virtual_lens.viewport(
        {**CENTRE, "pan": 19246, "tilt": 12351}
    )

    assert (centred.width, centred.height) == (1280, 720)
    assert (virtual_lens.VIRTUAL_WORLD_WIDTH, virtual_lens.VIRTUAL_WORLD_HEIGHT) == (4493, 1387)
    assert quarter_relative.x - centred.x == 160
    assert quarter_relative.y - centred.y == -90
    # Moving the sampled viewport +160,-90 moves visible content -160,+90.


def test_onvif_up_moves_the_viewport_up_and_content_down() -> None:
    centred = virtual_lens.viewport(CENTRE)
    onvif_up = virtual_lens.viewport({**CENTRE, "tilt": 10405})

    assert onvif_up.y - centred.y == -360


def test_tele_zoom_uses_measured_g5_field_of_view() -> None:
    wide = virtual_lens.viewport(CENTRE)
    tele = virtual_lens.viewport({**CENTRE, "zoom": 730})

    assert (tele.width, tele.height) == (584, 352)
    assert round(wide.width / tele.width, 3) == 2.192
    assert round(wide.height / tele.height, 3) == 2.045


def test_texture_wrap_and_reflection_are_deterministic() -> None:
    frame = bytearray(virtual_lens.TEXTURE_WIDTH * virtual_lens.TEXTURE_HEIGHT * 3)
    for y in range(virtual_lens.TEXTURE_HEIGHT):
        for x in range(virtual_lens.TEXTURE_WIDTH):
            offset = (y * virtual_lens.TEXTURE_WIDTH + x) * 3
            frame[offset : offset + 3] = bytes((x % 256, y % 256, 0))
    ppm = virtual_lens.crop_ppm(
        bytes(frame), virtual_lens.Viewport(x=1279, y=-1, width=4, height=2)
    )
    body = ppm.split(b"\n", 3)[3]

    assert body[:12] == bytes((127, 103, 0, 128, 103, 0, 129, 103, 0, 130, 103, 0))
    assert body[12:24] == bytes((127, 104, 0, 128, 104, 0, 129, 104, 0, 130, 104, 0))


def test_centre_wide_viewport_is_exactly_the_original_frame() -> None:
    frame = bytes(
        value % 251
        for value in range(virtual_lens.TEXTURE_WIDTH * virtual_lens.TEXTURE_HEIGHT * 3)
    )

    ppm = virtual_lens.crop_ppm(frame, virtual_lens.viewport(CENTRE))

    assert ppm.split(b"\n", 3)[3] == frame


def test_atomic_state_exposes_motor_and_viewport(tmp_path: Path) -> None:
    state = tmp_path / "lens.json"
    source = virtual_lens.LensSource(
        tmp_path / "unused.mp4", 5, 1280, 720, gimbal.Gimbal(), state_path=state
    )
    crop = virtual_lens.viewport(CENTRE)

    source._publish(crop, CENTRE)

    written = json.loads(state.read_text(encoding="utf-8"))
    assert written["motor"]["pan"] == 18000
    assert written["viewport"] == {"x": -640, "y": -360, "width": 1280, "height": 720}
    assert written["texture"]["mode"] == "horizontal-wrap-vertical-reflect"
    assert list(tmp_path.glob(".lens.json.*.tmp")) == []


def test_persistent_encoder_accepts_different_crop_sizes() -> None:
    texture = bytes(virtual_lens.TEXTURE_WIDTH * virtual_lens.TEXTURE_HEIGHT * 3)
    wide = virtual_lens.crop_ppm(texture, virtual_lens.viewport(CENTRE))
    tele = virtual_lens.crop_ppm(texture, virtual_lens.viewport({**CENTRE, "zoom": 730}))

    result = subprocess.run(
        virtual_lens.encoder_command(5, 320, 180),
        input=wide + tele,
        capture_output=True,
        check=True,
        timeout=20,
    )
    units = list(annexb.annex_b_units(result.stdout))
    kinds = [hevc.nal_type(unit) for unit in units]

    assert {hevc.NAL_VPS, hevc.NAL_SPS, hevc.NAL_PPS}.issubset(kinds)
    assert sum(kind is not None and kind <= annexb.VCL_MAX for kind in kinds) == 2


def test_real_pipeline_can_close_and_restart(tmp_path: Path) -> None:
    fixture = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc2=size=320x180:rate=2", "-t", "1", "-an",
            "-c:v", "mpeg4", "-y", str(fixture),
        ],
        check=True,
        timeout=20,
    )
    source = virtual_lens.LensSource(fixture, 2, 320, 180, gimbal.Gimbal())

    for _ in range(2):
        assert source.hvcc()
        pictures = source.pictures()
        _, keyframe = next(pictures)
        assert keyframe
        started = time.monotonic()
        source.close()
        assert time.monotonic() - started < 8.0


def test_close_cannot_be_overtaken_by_a_blocked_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_probe = threading.Event()
    release_probe = threading.Event()
    close_started = threading.Event()
    close_finished = threading.Event()

    def probe(_path: Path) -> tuple[int, int]:
        entered_probe.set()
        assert release_probe.wait(1.0)
        return virtual_lens.TEXTURE_WIDTH, virtual_lens.TEXTURE_HEIGHT

    class Process:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = 0

        def poll(self) -> int:
            return 0

    source = virtual_lens.LensSource(
        Path("source.mp4"), 15, 1280, 720, gimbal.Gimbal()
    )
    monkeypatch.setattr(virtual_lens, "probe_dimensions", probe)
    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(source, "_feed", lambda *_args: None)
    monkeypatch.setattr(source, "_read_units", lambda *_args: None)
    monkeypatch.setattr(source, "_drain", lambda *_args: None)

    starter = threading.Thread(target=source._start)

    def close() -> None:
        close_started.set()
        source.close()
        close_finished.set()

    closer = threading.Thread(target=close)
    starter.start()
    assert entered_probe.wait(1.0)
    closer.start()
    assert close_started.wait(1.0)
    assert not close_finished.wait(0.05)
    release_probe.set()
    starter.join(1.0)
    closer.join(1.0)

    assert close_finished.is_set()
    assert source._stopping.is_set()
    assert source._decoder is None
    assert source._encoder is None
