from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


TRAFFIC = Path("/state/finch-messages.jsonl")
REQUIRED_SETUP = {"ChangeVideoSettings", "EnablePtzControl"}
HEARTBEAT = "ubnt_avclient_timeSync"


def messages() -> list[dict[str, Any]]:
    if not TRAFFIC.is_file() or time.time() - TRAFFIC.stat().st_mtime > 30:
        return []
    messages: list[dict[str, Any]] = []
    for raw in TRAFFIC.read_text().splitlines():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            messages.append(value)
    return messages


def main() -> int:
    traffic = messages()
    if not traffic:
        return 1
    names = {str(message.get("functionName", "")) for message in traffic}
    recent_names = {
        str(message.get("functionName", "")) for message in traffic[-100:]
    }
    # Setup verbs are one-time evidence and naturally age out of a tail window.
    # The file mtime plus a recent time-sync proves the live control session.
    return 0 if REQUIRED_SETUP <= names and HEARTBEAT in recent_names else 1


if __name__ == "__main__":
    raise SystemExit(main())
