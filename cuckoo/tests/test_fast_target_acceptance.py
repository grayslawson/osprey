from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_PATH = Path("/opt/lab/fast_target_acceptance.py")
if not _PATH.exists():
    _PATH = Path(__file__).parents[2] / "docker" / "fast_target_acceptance.py"
_SPEC = importlib.util.spec_from_file_location("fast_target_acceptance", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_fast_target_trace_passes_with_bounded_latency() -> None:
    result = _MODULE.evaluate([
        {"stage": "detection", "ts": 10.0},
        {"stage": "command", "ts": 10.35},
        {"stage": "movement", "predicted": 0.7, "actual": 0.8, "ts": 10.4},
    ])
    assert result["pass"] is True
    assert result["command_latency_max"] == pytest.approx(0.35)


def test_fast_target_trace_rejects_stale_queue_latency_and_bad_weights() -> None:
    result = _MODULE.evaluate([
        {"stage": "detection", "ts": 10.0},
        {"stage": "command", "ts": 15.0},
        {"stage": "movement", "predicted": 0.089, "actual": 0.754, "ts": 15.1},
    ])
    assert result["pass"] is False
    assert result["command_latency_max"] == 5.0
    assert result["timing_ratio_max"] > 8.0
