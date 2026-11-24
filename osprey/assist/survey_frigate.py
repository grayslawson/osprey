"""Frigate's event history as a map of where things happen. Read only; stdlib only."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Final

from . import survey_plan as sp
from .config import Frigate
from .survey_store import SurveyStore

BLOCK: Final = 10.0


class FrigateClient:
    def __init__(self, cfg: Frigate, timeout: float = 15.0) -> None:
        self.cfg, self.timeout = cfg, timeout

    def events(self, after: float, limit: int = 1000) -> list[dict[str, Any]]:
        query = {"after": f"{after:.0f}", "limit": str(limit), "has_clip": "0"}
        if self.cfg.camera:
            query["cameras"] = self.cfg.camera
        headers = {"Accept": "application/json"}
        if self.cfg.api_key_file:
            try:
                headers["Authorization"] = "Bearer " + Path(self.cfg.api_key_file).read_text(encoding="utf-8").strip()
            except OSError:
                raise ConnectionError("Frigate key file unreadable") from None
        req = urllib.request.Request(f"{self.cfg.url}/api/events?{urllib.parse.urlencode(query)}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as reply:  # noqa: S310 - operator-set URL
                data = json.loads(reply.read(8 * 1024 * 1024))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ConnectionError(f"Frigate: {type(exc).__name__}") from None
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def event_center(event: dict[str, Any]) -> tuple[float, float] | None:
    """The middle of the event's box as picture fractions (x right, y down), or None when it has none."""
    data = event.get("data")
    box = data.get("box") if isinstance(data, dict) else None
    if (not isinstance(box, list) or len(box) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in box)):
        return None
    x, y, w, h = (float(v) for v in box)
    cx, cy = x + w / 2, y + h / 2
    return (cx, cy) if 0 <= cx <= 1 and 0 <= cy <= 1 else None


def refresh_heat(store: SurveyStore, client: FrigateClient, camera: str, geo: sp.Geometry, cfg: Frigate,
                 now: float, projection: str = "rectilinear") -> dict[str, int]:
    """Add the events since the last refresh to the heat map. Returns how many were placed and skipped."""
    cursor_key = f"survey_heat_cursor:{camera}"
    after = float(store.s.get(cursor_key, now - cfg.lookback_days * 86400))
    placed = skipped = 0
    latest = after
    for event in client.events(after):
        start = event.get("start_time")
        if not isinstance(start, (int, float)) or start <= after:
            continue
        latest = max(latest, float(start))
        center = event_center(event)
        pos = store.position_at(camera, float(start))
        if center is None or pos is None:
            skipped += 1
            continue
        fov = geo.fov(pos[2])
        dx, dy = sp.to_angles(2 * center[0] - 1, 1 - 2 * center[1], fov, projection)
        az, el = geo.az(pos[0]) + dx, geo.el(pos[1]) + dy
        store.add_heat(camera, math.floor(az / BLOCK), math.floor(el / BLOCK), 1.0, cfg.half_life_days)
        placed += 1
    store.s.put(cursor_key, max(latest, after))
    return {"placed": placed, "skipped": skipped}
