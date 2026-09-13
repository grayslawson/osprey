"""Regression checks for the guarded Frigate recovery patch."""

from __future__ import annotations

from pathlib import Path


_CANDIDATES = (
    Path("/opt/lab/patch_frigate_ptz_recovery.py"),
    Path(__file__).parents[2] / "docker" / "patch_frigate_ptz_recovery.py",
)
SOURCE_PATH = next(path for path in _CANDIDATES if path.is_file())
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")


def test_relative_move_fault_always_clears_active() -> None:
    block = SOURCE[SOURCE.index("def patch_onvif"):SOURCE.index("def patch_autotrack")]
    assert "RelativeMove(move_request)" in block
    assert "finally:" in block
    assert 'self.cams[camera_name]["active"] = False' in block


def test_goto_preset_fault_always_clears_active() -> None:
    block = SOURCE[SOURCE.index("def patch_onvif"):SOURCE.index("def patch_autotrack")]
    assert "GotoPreset(" in block
    assert block.count("finally:") >= 2
    assert block.count('self.cams[camera_name]["active"] = False') >= 2


def test_motor_wait_is_bounded_and_timeout_is_visible() -> None:
    assert "async def _wait_for_motor_stop" in SOURCE
    assert "timeout=15.0" in SOURCE
    assert "asyncio.wait_for(" in SOURCE
    assert "timed out waiting for truthful IDLE" in SOURCE


def test_queue_recovers_after_move_exception_and_cancellation() -> None:
    assert "async def _process_one_move" in SOURCE
    assert "except asyncio.CancelledError" in SOURCE
    assert "logger.exception(" in SOURCE
    assert 'self.cams[camera_name]["active"] = False' in SOURCE


def test_home_return_serializes_with_move_queue_and_is_cancellable() -> None:
    maintenance = SOURCE[SOURCE.index("tracking_active.clear"):]
    assert "async with self.move_queue_locks[camera]" in maintenance
    assert "return_preset" in maintenance
    assert "_wait_for_motor_stop(camera)" in maintenance
    assert "asyncio.CancelledError" in maintenance
