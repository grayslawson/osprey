"""Shared fixtures. Not a test module — helpers only, so every test builds the
same camera and a drift in one place shows up everywhere.
"""

from __future__ import annotations
import os
import stat
from pathlib import Path

import adoption
from model import AxisRange, Camera, Position


def assert_no_secret(text: str, *secrets: str) -> None:
    """Assert that operator-visible output does not contain credential values."""
    for secret in secrets:
        assert secret and secret not in text


def doctor_fixture_environment(tmp_path: Path, *, curl_ok: bool = True) -> dict[str, str]:
    """Return a deterministic PATH containing the host tools doctor inspects."""
    tools = tmp_path / "doctor-tools"
    tools.mkdir()
    (tools / "docker").write_text("#!/bin/sh\n[ \"$1 $2\" = 'compose version' ]\n", encoding="utf-8")
    (tools / "ip").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "ss").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "curl").write_text(
        f"#!/bin/sh\n{'exit 0' if curl_ok else 'exit 1'}\n", encoding="utf-8"
    )
    for tool in tools.iterdir():
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{tools}:{os.environ.get('PATH', '')}"}

PAN_RANGE = AxisRange(500, 35500)
TILT_RANGE = AxisRange(8000, 18000)
ZOOM_RANGE = AxisRange(0, 730)


def a_camera(ptz: bool = True) -> Camera:
    """An adopted G5 PTZ, with the motor bounds the real one announces."""
    camera = Camera(
        mac="AABBCCDDEEFF",
        model="UVC G5 PTZ",
        firmware="5.3.95",
        name="front gate",
        adopted=True,
        tracks=adoption.track_defaults(),
    )
    if ptz:
        camera.pan_range, camera.tilt_range, camera.zoom_range = PAN_RANGE, TILT_RANGE, ZOOM_RANGE
    camera.motion.update(Position(pan=18000, tilt=13000, zoom=365, focus=50), activity=0)
    camera.motion.connect()
    return camera
