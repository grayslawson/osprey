"""Offline fast-target latency acceptance check.

Consumes a JSON-lines trace exported from Frigate/Cuckoo.  It deliberately
does not command a camera, making it safe to run against recorded car/person
events or synthetic traces.  Each record must contain ``stage`` and ``ts``
(monotonic seconds); movement records may also contain ``predicted`` and
``actual`` settle durations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def evaluate(records: list[dict[str, Any]], *, max_command_latency: float = 1.0,
             max_timing_ratio: float = 2.0) -> dict[str, Any]:
    detections = [r for r in records if r.get("stage") == "detection"]
    commands = [r for r in records if r.get("stage") == "command"]
    latencies: list[float] = []
    for detection in detections:
        later = [float(c["ts"]) for c in commands if float(c["ts"]) >= float(detection["ts"])]
        if later:
            latencies.append(min(later) - float(detection["ts"]))
    ratios = [
        float(r["actual"]) / float(r["predicted"])
        for r in records
        if r.get("stage") == "movement"
        and float(r.get("predicted", 0)) > 0
        and float(r.get("actual", 0)) >= 0
    ]
    result = {
        "detections": len(detections),
        "commands": len(commands),
        "command_latency_max": max(latencies, default=None),
        "timing_ratio_max": max(ratios, default=None),
        "pass": bool(detections and commands and latencies
                     and max(latencies) <= max_command_latency
                     and (not ratios or max(ratios) <= max_timing_ratio)),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="JSON-lines latency trace")
    parser.add_argument("--max-command-latency", type=float, default=1.0)
    parser.add_argument("--max-timing-ratio", type=float, default=2.0)
    args = parser.parse_args()
    records = [json.loads(line) for line in args.trace.read_text().splitlines() if line.strip()]
    result = evaluate(records, max_command_latency=args.max_command_latency,
                      max_timing_ratio=args.max_timing_ratio)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
