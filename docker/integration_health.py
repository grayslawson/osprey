"""Read-only Cuckoo + Frigate integration reconciliation.

This intentionally does not move a camera or mutate Frigate.  It is safe to run
after either container restarts and turns configuration drift into an unhealthy
dependency with an actionable message.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


def _get_json(url: str, timeout: float = 3.0) -> Any:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is operator config
        return json.load(response)


def validate_config(config: dict[str, Any], camera: str = "g5_ptz") -> list[str]:
    """Return config mismatches; an empty list means the integration reconciles."""
    errors: list[str] = []
    cameras = config.get("cameras")
    if not isinstance(cameras, dict) or not isinstance(cameras.get(camera), dict):
        return [f"camera {camera!r} is missing from Frigate config"]
    item = cameras[camera]
    inputs = item.get("ffmpeg", {}).get("inputs", [])
    paths = {str(entry.get("path")) for entry in inputs if isinstance(entry, dict)}
    expected_path = os.environ.get("CUCKOO_RTSP_URI", "rtsp://cuckoo:8554/video2")
    if expected_path not in paths:
        errors.append(f"RTSP input {expected_path!r} is missing")
    onvif = item.get("onvif", {})
    expected = {
        "host": os.environ.get("CUCKOO_ONVIF_HOST", "cuckoo"),
        "port": int(os.environ.get("CUCKOO_ONVIF_PORT", "8000")),
        "profile": os.environ.get("CUCKOO_ONVIF_PROFILE", "video2"),
    }
    for key, value in expected.items():
        if onvif.get(key) != value:
            errors.append(f"ONVIF {key}={onvif.get(key)!r}, expected {value!r}")
    tracking = onvif.get("autotracking", {})
    if tracking.get("enabled") is not True:
        errors.append("ONVIF autotracking is not enabled")
    if tracking.get("return_preset") != os.environ.get("CUCKOO_RETURN_PRESET", "home"):
        errors.append("ONVIF return_preset is not the configured home preset")
    return errors


def main() -> int:
    frigate_url = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
    cuckoo_host = os.environ.get("CUCKOO_HOST", "cuckoo")
    cuckoo_port = int(os.environ.get("CUCKOO_PORT", "8000"))
    try:
        with socket.create_connection((cuckoo_host, cuckoo_port), timeout=2):
            pass
        _get_json(f"{frigate_url}/api/version")
        config = _get_json(f"{frigate_url}/api/config")
        if not isinstance(config, dict):
            raise ValueError("Frigate config response is not an object")
        errors = validate_config(config, os.environ.get("FRIGATE_CAMERA", "g5_ptz"))
        if errors:
            raise ValueError("; ".join(errors))
    except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"integration reconciliation failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print("integration reconciliation passed: Cuckoo reachable and Frigate PTZ wiring matches", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
