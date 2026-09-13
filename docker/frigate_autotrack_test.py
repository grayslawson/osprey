"""Correlate Frigate autotracking with Cuckoo and Finch PTZ traffic.

The physical profile intentionally uses absolute zoom.  Keep this verifier
mode-agnostic: relative zoom is useful for synthetic experiments, while
absolute zoom is the supported G5 production path.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
CAMERA = os.environ.get("FRIGATE_CAMERA", "synthetic_g5_ptz")
LABEL = os.environ.get("FRIGATE_LABEL", "person")
ZONE = os.environ.get("FRIGATE_ZONE", "tracking_zone")
TIMEOUT = float(os.environ.get("FRIGATE_AUTOTRACK_TIMEOUT", "120"))
CUCKOO_TRAFFIC = Path("/cuckoo-state/cuckoo-messages.jsonl")
FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
RELATIVE_MOVE = re.compile(
    rf"{re.escape(CAMERA)} called RelativeMove: "
    rf"pan: (?P<pan>{FLOAT}) tilt: (?P<tilt>{FLOAT}) zoom: (?P<zoom>{FLOAT})"
)


def get_json(path: str) -> Any:
    request = urllib.request.Request(
        f"{BASE_URL}{path}", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def messages() -> list[dict[str, Any]]:
    if not CUCKOO_TRAFFIC.is_file():
        return []
    parsed: list[dict[str, Any]] = []
    for line in CUCKOO_TRAFFIC.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            parsed.append(value)
    return parsed


def count_traffic(items: list[dict[str, Any]], name: str, direction: str) -> int:
    return sum(
        item.get("functionName") == name and item.get("direction") == direction
        for item in items
    )


def preset_zoom_targets(items: list[dict[str, Any]]) -> list[float]:
    targets: list[float] = []
    for item in items:
        if item.get("functionName") != "Preset" or item.get("direction") != "out":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict) or payload.get("action") != "config":
            continue
        presets = payload.get("items")
        if not isinstance(presets, list):
            continue
        for preset in presets:
            if isinstance(preset, dict) and isinstance(preset.get("zoom"), (int, float)):
                targets.append(float(preset["zoom"]))
    return targets


def object_events() -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {"camera": CAMERA, "label": LABEL, "limit": "30", "include_thumbnails": "0"}
    )
    response = get_json(f"/api/events?{query}")
    return response if isinstance(response, list) else []


def qualifying_event(
    events: list[dict[str, Any]], baseline_ids: set[str]
) -> dict[str, Any] | None:
    for event in events:
        data = event.get("data")
        if (
            str(event.get("id")) not in baseline_ids
            and event.get("camera") == CAMERA
            and event.get("label") == LABEL
            and ZONE in event.get("zones", [])
            and isinstance(data, dict)
            and data.get("type") == "object"
            and float(data.get("top_score") or 0) >= 0.5
        ):
            return event
    return None


def nonzero_relative_zoom(lines: list[Any]) -> tuple[float, str] | None:
    for value in lines:
        line = str(value)
        match = RELATIVE_MOVE.search(line)
        if match is None:
            continue
        zoom = float(match.group("zoom"))
        if abs(zoom) > 1e-6:
            return zoom, line
    return None


def main() -> int:
    config = get_json("/api/config")
    autotracking = (
        config.get("cameras", {})
        .get(CAMERA, {})
        .get("onvif", {})
        .get("autotracking", {})
    )
    if not (
        autotracking.get("enabled")
        or autotracking.get("enabled_in_config")
    ):
        raise SystemExit(f"autotracking is disabled in Frigate config for {CAMERA}")
    zoom_mode = autotracking.get("zooming", "disabled")
    if zoom_mode not in {"absolute", "relative", "disabled"}:
        raise SystemExit(
            f"unsupported Frigate zooming mode: {zoom_mode!r}"
        )

    ptz_info = get_json(
        f"/api/{urllib.parse.quote(CAMERA, safe='')}/ptz/info"
    )
    presets = ptz_info.get("presets", []) if isinstance(ptz_info, dict) else []
    if "home" not in presets:
        raise SystemExit(
            "Frigate has no home preset. Run "
            "'docker compose --profile frigate run --rm frigate-init', then "
            "recreate Frigate before this verifier."
        )

    baseline_ids = {str(event.get("id")) for event in object_events()}
    traffic_before = messages()
    preset_before = count_traffic(traffic_before, "Preset", "out")
    motor_before = count_traffic(traffic_before, "EventMotorState", "in")
    prior_zoom_targets = preset_zoom_targets(traffic_before)
    initial_zoom_target = prior_zoom_targets[-1] if prior_zoom_targets else 0.0
    initial_logs = get_json("/api/logs/frigate?start=-1")
    log_start = int(initial_logs.get("totalLines") or 0)
    deadline = time.monotonic() + TIMEOUT
    last_detail = "waiting for a new synthetic object pass"

    while time.monotonic() < deadline:
        try:
            event = qualifying_event(object_events(), baseline_ids)
            logs = get_json(f"/api/logs/frigate?start={log_start}")
            lines = logs.get("lines", []) if isinstance(logs, dict) else []
            saw_object_lifecycle = bool(
                event
                and any(
                    ("New object:" in str(line) or "Reacquired object:" in str(line))
                    and CAMERA in str(line)
                    and str(event.get("id")) in str(line)
                    for line in lines
                )
            )
            relative_zoom = nonzero_relative_zoom(lines)
            traffic_after = messages()
            new_traffic = traffic_after[len(traffic_before) :]
            preset_after = count_traffic(traffic_after, "Preset", "out")
            motor_after = count_traffic(traffic_after, "EventMotorState", "in")
            new_zoom_targets = preset_zoom_targets(new_traffic)
            zoom_target_changed = any(
                abs(target - initial_zoom_target) >= 1.0 for target in new_zoom_targets
            )

            if (
                event is not None
                and saw_object_lifecycle
                and (
                    zoom_mode == "disabled"
                    or relative_zoom is not None
                    or zoom_target_changed
                )
                and preset_after >= preset_before + 2
                and motor_after > motor_before
                and zoom_target_changed
            ):
                data = event["data"]
                evidence = {
                    "camera": CAMERA,
                    "event_id": event.get("id"),
                    "top_score": data.get("top_score"),
                    "zones": event.get("zones"),
                    "frigate_zooming_mode": zoom_mode,
                    "frigate_relative_zoom_request": (
                        relative_zoom[0] if relative_zoom else None
                    ),
                    "cuckoo_initial_zoom_target": initial_zoom_target,
                    "cuckoo_new_zoom_target_count": len(new_zoom_targets),
                    "cuckoo_new_zoom_targets_last_10": new_zoom_targets[-10:],
                    "cuckoo_preset_messages_added": preset_after - preset_before,
                    "finch_motor_events_added": motor_after - motor_before,
                }
                print("Frigate relative-zoom autotracking path verified:")
                print(json.dumps(evidence, indent=2, sort_keys=True))
                return 0

            last_detail = (
                f"event={event.get('id') if event else None}, "
                f"object_lifecycle={saw_object_lifecycle}, "
                f"zoom_mode={zoom_mode}, "
                f"relative_zoom={relative_zoom[0] if relative_zoom else None}, "
                f"Preset delta={preset_after - preset_before}, "
                f"EventMotorState delta={motor_after - motor_before}, "
                f"new zoom target count={len(new_zoom_targets)}, "
                f"last targets={new_zoom_targets[-5:]}, "
                f"initial zoom target={initial_zoom_target}"
            )
        except (OSError, TypeError, ValueError, urllib.error.URLError) as exc:
            last_detail = f"transient API or data error: {exc}"
            time.sleep(2)

    raise SystemExit(f"timed out after {TIMEOUT:.0f}s: {last_detail}")


if __name__ == "__main__":
    raise SystemExit(main())
