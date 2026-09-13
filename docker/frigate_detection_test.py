"""Bounded Frigate API assertion for the synthetic person fixture.

This intentionally checks Frigate's own event API, not merely that frames are
arriving.  A successful exit means the running Frigate process created a
person event for the lab camera during its current lifetime.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


BASE_URL = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
CAMERA = os.environ.get("FRIGATE_CAMERA", "synthetic_g5_ptz")
LABEL = os.environ.get("FRIGATE_LABEL", "person")
ZONE = os.environ.get("FRIGATE_ZONE", "tracking_zone")
TIMEOUT = float(os.environ.get("FRIGATE_DETECTION_TIMEOUT", "90"))


def get_json(path: str) -> Any:
    request = urllib.request.Request(
        f"{BASE_URL}{path}", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def matching_event(events: list[dict[str, Any]], service_started: float) -> dict[str, Any] | None:
    for event in events:
        if event.get("camera") != CAMERA or event.get("label") != LABEL:
            continue
        data = event.get("data")
        if not isinstance(data, dict) or data.get("type") != "object":
            continue
        if float(data.get("top_score") or 0) < 0.5:
            continue
        if ZONE not in event.get("zones", []):
            continue
        if not event.get("has_snapshot"):
            continue
        start_time = float(event.get("start_time") or 0)
        if start_time >= service_started - 5:
            return event
    return None


def main() -> int:
    deadline = time.monotonic() + TIMEOUT
    service_started: float | None = None
    last_error = "Frigate API has not answered yet"
    query = urllib.parse.urlencode(
        {"camera": CAMERA, "label": LABEL, "limit": "20", "include_thumbnails": "0"}
    )

    while time.monotonic() < deadline:
        try:
            stats = get_json("/api/stats")
            camera_stats = stats.get("cameras", {}).get(CAMERA, {})
            service_stats = stats.get("service", {})
            uptime = float(service_stats.get("uptime") or 0)
            if service_started is None and uptime > 0:
                service_started = time.time() - uptime

            if not camera_stats.get("detection_enabled"):
                last_error = f"detection is disabled for {CAMERA}"
            elif float(camera_stats.get("camera_fps") or 0) <= 0:
                last_error = f"{CAMERA} has no incoming frames"
            elif service_started is not None:
                events = get_json(f"/api/events?{query}")
                if isinstance(events, list):
                    event = matching_event(events, service_started)
                    if event is not None:
                        data = event["data"]
                        evidence = {
                            "camera": event.get("camera"),
                            "event_id": event.get("id"),
                            "label": event.get("label"),
                            "top_score": data.get("top_score"),
                            "zones": event.get("zones"),
                            "has_snapshot": event.get("has_snapshot"),
                            "start_time": event.get("start_time"),
                            "end_time": event.get("end_time"),
                            "camera_fps": camera_stats.get("camera_fps"),
                            "detection_fps": camera_stats.get("detection_fps"),
                        }
                        print("Frigate person event verified:")
                        print(json.dumps(evidence, indent=2, sort_keys=True))
                        return 0
                    last_error = f"no current-instance {LABEL} event in Frigate API ({len(events)} returned)"
                else:
                    last_error = "Frigate events endpoint returned a non-list response"
        except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
            last_error = f"Frigate API error: {exc}"

        time.sleep(2)

    raise SystemExit(f"timed out after {TIMEOUT:.0f}s: {last_error}")


if __name__ == "__main__":
    raise SystemExit(main())
