from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Compose mounts the lab helpers at /opt/lab, while a host test imports them
# from the repository.  Keep the test valid in both supported environments.
_helpers = Path("/opt/lab")
if not _helpers.exists():
    _helpers = Path(__file__).parents[2] / "docker"
sys.path.insert(0, str(_helpers))

from integration_health import validate_config  # type: ignore[import-not-found]


def config() -> dict[str, Any]:
    return {
        "cameras": {
            "g5_ptz": {
                "ffmpeg": {"inputs": [{"path": "rtsp://cuckoo:8554/video2"}]},
                "onvif": {
                    "host": "cuckoo", "port": 8000, "profile": "video2",
                    "autotracking": {"enabled": True, "return_preset": "home"},
                },
            }
        }
    }


def test_reconciled_config_has_no_drift() -> None:
    assert validate_config(config()) == []


def test_reconciler_reports_restart_drift() -> None:
    current = config()
    current["cameras"]["g5_ptz"]["onvif"]["autotracking"]["return_preset"] = "old-home"
    errors = validate_config(current)
    assert any("return_preset" in error for error in errors)


def test_reconciler_reports_missing_camera() -> None:
    assert validate_config({}) == ["camera 'g5_ptz' is missing from Frigate config"]
