"""Turn a labelled map into proposals: presets, a patrol route, night views and Frigate suggestions.

Pure given its inputs. It reads regions (dicts from the label grid), the camera geometry and the
panorama, and never touches a camera.
"""

from __future__ import annotations

import math
from typing import Any, Final

from PIL import Image

from . import survey_plan as sp
from .survey_map import LabelGrid, Mosaic
from .survey_understand import INTEREST_LABELS

NAMES: Final = {"door": "Door", "gate": "Gate", "driveway": "Driveway", "path": "Path", "street": "Street",
                "yard": "Yard", "fence_line": "Fence Line"}
ZONE_LABELS: Final = ("door", "gate", "driveway", "path", "street")
MASK_LABELS: Final = ("sky", "foliage")
NAME_MAX: Final = 64


def view_of(geo: sp.Geometry, pan: float, tilt: float, zoom: float) -> dict[str, Any]:
    fh, fv = geo.fov(zoom)
    return {"az": geo.az(pan), "el": geo.el(tilt), "fh": fh, "fv": fv}


def _near(a: dict[str, Any], b: dict[str, Any], frac: float) -> bool:
    return bool(abs(a["az"] - b["az"]) <= frac * min(a["fh"], b["fh"])
                and abs(a["el"] - b["el"]) <= frac * min(a["fv"], b["fv"]))


def preset_proposals(geo: sp.Geometry, regions: list[dict[str, Any]], existing: list[dict[str, Any]], *,
                     max_presets: int = 10, min_interest: float = 0.35, zoom_max: float = 0.6) -> list[dict[str, Any]]:
    """One proposal per interesting region (or per view of a wide one), skipping what an existing preset covers.

    ``existing`` rows carry ``name`` and a normalised ``position`` (pan, tilt, zoom). A proposal that an
    existing preset already covers comes back with ``covers`` set and ``action`` ``reuse``.
    """
    taken = {str(p["name"]).casefold() for p in existing}
    seen = [view_of(geo, p["position"]["pan"], p["position"]["tilt"], p["position"]["zoom"])
            | {"name": p["name"], "existing": True} for p in existing if isinstance(p.get("position"), dict)]
    wide_h = geo.fov(0.0)[0]
    out: list[dict[str, Any]] = []
    picks = sorted((r for r in regions if r["label"] in INTEREST_LABELS and r["interest"] >= min_interest),
                   key=lambda r: -r["interest"])
    fresh = 0
    for region in picks:
        az0, el0, az1, el1 = region["bbox"]
        parts = max(1, math.ceil((az1 - az0) / (0.8 * wide_h)))
        step = (az1 - az0) / parts
        for k in range(parts):
            pan, tilt, zoom = sp.frame_view(geo, az0 + k * step, el0, az0 + (k + 1) * step, el1,
                                            fill=0.6 if region["label"] in ("door", "gate") else 0.8, zoom_max=zoom_max)
            view = view_of(geo, pan, tilt, zoom)
            twin = next((s for s in seen if _near(view, s, 0.4)), None)
            if twin is not None:
                if twin.get("existing") and not any(o.get("covers") == twin["name"] for o in out):
                    out.append({"action": "reuse", "name": twin["name"], "covers": twin["name"], "label": region["label"],
                                "interest": region["interest"], "pan": round(pan, 5), "tilt": round(tilt, 5),
                                "zoom": round(zoom, 3), "region": region["id"], "reason": f"{twin['name']} already looks here"})
                continue
            if fresh >= max_presets:
                continue
            fresh += 1
            view["name"] = ""
            seen.append(view)
            out.append({"action": "create", "name": "", "covers": None, "label": region["label"],
                        "interest": region["interest"], "pan": round(pan, 5), "tilt": round(tilt, 5),
                        "zoom": round(zoom, 3), "region": region["id"], "az": round(view["az"], 2),
                        "el": round(view["el"], 2), "reason": _reason(region)})
    _name(out, taken)
    return out


def _reason(region: dict[str, Any]) -> str:
    why = f"{NAMES.get(region['label'], region['label'])} region, interest {region['interest']:.2f}"
    if region.get("heat"):
        why += f", seen activity ({region['heat']:.2f})"
    return why


def _name(items: list[dict[str, Any]], taken: set[str]) -> None:
    """Name by what is there; a number when several share a label (left to right); unique among all presets."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item["action"] == "create":
            groups.setdefault(item["label"], []).append(item)
    for label, group in groups.items():
        group.sort(key=lambda i: i["az"])
        base = NAMES.get(label, label.title())
        for n, item in enumerate(group, 1):
            name = f"{base} {n}" if len(group) > 1 else base
            while name.casefold() in taken:
                n += len(group)
                name = f"{base} {n}"
            taken.add(name.casefold())
            item["name"] = name[:NAME_MAX]


# ---------------------------------------------------------------------- route

def _order(points: list[dict[str, Any]], start: tuple[float, float] | None) -> list[dict[str, Any]]:
    """Nearest neighbour then 2-opt on head travel."""
    if len(points) < 3:
        return list(points)
    left, cur = list(points), start or (points[0]["az"], points[0]["el"])
    route: list[dict[str, Any]] = []
    while left:
        nxt = min(left, key=lambda p: sp.travel_deg(cur, (p["az"], p["el"])))
        left.remove(nxt)
        route.append(nxt)
        cur = (nxt["az"], nxt["el"])

    def length(r: list[dict[str, Any]]) -> float:
        return sum(sp.travel_deg((a["az"], a["el"]), (b["az"], b["el"])) for a, b in zip(r, r[1:] + r[:1], strict=True))

    improved = True
    while improved:
        improved = False
        for i in range(1, len(route) - 1):
            for j in range(i + 1, len(route)):
                cand = route[:i] + route[i:j + 1][::-1] + route[j + 1:]
                if length(cand) < length(route) - 1e-9:
                    route, improved = cand, True
    return route


def dwell_for(interest: float, mean: float, cfg: dict[str, float]) -> float:
    scale = 0.5 + 1.5 * (interest / mean if mean > 0 else 1.0)
    return round(max(cfg["min_sec"], min(cfg["max_sec"], cfg["base_sec"] * scale)), 1)


def route_proposal(stops: list[dict[str, Any]], tour: list[dict[str, Any]], policy: str, dwell_cfg: dict[str, float],
                   *, patrol_exists: bool) -> dict[str, Any]:
    """The patrol the survey recommends.

    ``stops`` are every place worth a stop, new or existing: name, az, el, interest, new. ``tour`` is the
    current patrol's points. With ``merge`` the user's points stay in their order and new stops go where they
    add the least travel; ``replace`` orders everything.
    """
    interests = [s["interest"] for s in stops] or [0.0]
    mean = sum(interests) / len(interests)
    by_name = {s["name"].casefold(): s for s in stops}
    dwell = {s["name"]: dwell_for(s["interest"], mean, dwell_cfg) for s in stops}
    new = [s for s in stops if s["new"]]
    if policy == "replace" or not tour:
        ordered = _order(stops, None)
        points = [{"name": s["name"], "dwell_sec": dwell[s["name"]]} for s in ordered]
        for p, old in ((p, next((t for t in tour if t["name"].casefold() == p["name"].casefold()), None)) for p in points):
            if old and old.get("night_preset"):
                p["night_preset"] = old["night_preset"]
    else:
        points = [dict(t) for t in tour]
        for stop in _order(new, None):
            def pos(p: dict[str, Any]) -> tuple[float, float] | None:
                s = by_name.get(str(p["name"]).casefold())
                return (s["az"], s["el"]) if s else None

            best, best_cost = len(points), math.inf
            for i in range(len(points) + 1):
                a = pos(points[i - 1]) if i > 0 else None
                b = pos(points[i]) if i < len(points) else None
                here = (stop["az"], stop["el"])
                cost = (sp.travel_deg(a, here) if a else 0) + (sp.travel_deg(here, b) if b else 0) \
                    - (sp.travel_deg(a, b) if a and b else 0)
                if cost < best_cost:
                    best, best_cost = i, cost
            points.insert(best, {"name": stop["name"], "dwell_sec": dwell[stop["name"]]})
    return {"points": points, "dwell": dwell, "policy": policy, "previous": [dict(t) for t in tour],
            "enable": not patrol_exists, "new": [s["name"] for s in new]}


# -------------------------------------------------------------- night variants

def _glare(canvas: Image.Image, box: tuple[int, int, int, int]) -> float:
    gray = canvas.convert("L").crop(box).resize((64, 36))
    data = gray.tobytes()
    return sum(1 for v in data if v >= 245) / len(data)


def night_variant(mosaic: Mosaic, view: dict[str, Any], max_frac: float = 0.04, min_glare: float = 0.03,
                  ) -> dict[str, Any] | None:
    """Look in the night panorama for a nearby place where the glare is at least halved. No camera move."""
    geo = mosaic.geo

    def box(az: float, el: float) -> tuple[int, int, int, int]:
        x, y = mosaic.px(az - view["fh"] / 2, el + view["fv"] / 2)
        return x, y, x + round(view["fh"] * mosaic.ppd), y + round(view["fv"] * mosaic.ppd)

    base = _glare(mosaic.canvas, box(view["az"], view["el"]))
    if base < min_glare:
        return None
    best: dict[str, Any] | None = None
    for size in (0.01, 0.02, 0.03, 0.04):
        if size > max_frac + 1e-9:
            continue
        for sx, sy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)):
            az = view["az"] + sx * size * geo.pan_deg
            el = view["el"] + sy * size * geo.tilt_deg
            if abs(az) > geo.pan_deg / 2 or abs(el) > geo.tilt_deg / 2:
                continue
            g = _glare(mosaic.canvas, box(az, el))
            if g <= base * 0.5 and (best is None or g < best["glare_after"] - 0.005):
                best = {"az": az, "el": el, "pan": round(geo.pan_of(az), 5), "tilt": round(geo.tilt_of(el), 5),
                        "glare_before": round(base, 3), "glare_after": round(g, 3), "offset": [sx * size, sy * size]}
        if best is not None:
            break    # the smallest step that works
    return best


# --------------------------------------------------------------------- frigate

def frigate_suggestions(geo: sp.Geometry, grid: LabelGrid, stops: list[dict[str, Any]], camera: str,
                        projection: str = "rectilinear", night: bool = False) -> dict[str, Any]:
    """Zones and motion masks as Frigate wants them, per stop. A suggestion file; nothing is written to Frigate."""
    zones = grid.components(ZONE_LABELS)
    masks = grid.components(MASK_LABELS)
    out: dict[str, Any] = {}
    for stop in stops:
        view = view_of(geo, stop["pan"], stop["tilt"], stop["zoom"])
        entry: dict[str, Any] = {"zones": {}, "motion_masks": []}
        counts: dict[str, int] = {}
        for kind, regions in (("zones", zones), ("masks", masks)):
            for r in regions:
                rect = _project(r["bbox"], view, geo, projection)
                if rect is None:
                    continue
                coords = f"{rect[0]:.3f},{rect[1]:.3f},{rect[2]:.3f},{rect[1]:.3f},{rect[2]:.3f},{rect[3]:.3f},{rect[0]:.3f},{rect[3]:.3f}"
                if kind == "zones":
                    n = counts[r["label"]] = counts.get(r["label"], 0) + 1
                    entry["zones"][r["label"] if n == 1 else f"{r['label']}_{n}"] = {"coordinates": coords}
                else:
                    entry["motion_masks"].append(coords)
        out[stop["name"]] = entry
    return {"note": "Suggestions only. Osprey never edits Frigate. Frigate zones and masks are drawn in frame "
                    "coordinates, so each set is right only at its own preset. Check them on a live picture before use.",
            "camera": camera, "map": "night" if night else "day", "presets": out, "yaml": _yaml(camera, out)}


def _project(bbox: list[float], view: dict[str, Any], geo: sp.Geometry, projection: str) -> tuple[float, float, float, float] | None:
    az0, el0, az1, el1 = bbox
    if az1 < view["az"] - view["fh"] / 2 or az0 > view["az"] + view["fh"] / 2:
        return None
    if el1 < view["el"] - view["fv"] / 2 or el0 > view["el"] + view["fv"] / 2:
        return None
    fov = (view["fh"], view["fv"])
    a0, a1 = max(az0, view["az"] - fov[0] / 2 + 0.01), min(az1, view["az"] + fov[0] / 2 - 0.01)
    e0, e1 = max(el0, view["el"] - fov[1] / 2 + 0.01), min(el1, view["el"] + fov[1] / 2 - 0.01)
    x0, y1n = sp.from_angles(a0 - view["az"], e0 - view["el"], fov, projection)
    x1, y0n = sp.from_angles(a1 - view["az"], e1 - view["el"], fov, projection)
    rect = ((x0 + 1) / 2, (1 - y0n) / 2, (x1 + 1) / 2, (1 - y1n) / 2)
    return rect if (rect[2] - rect[0]) * (rect[3] - rect[1]) >= 0.04 else None


def _yaml(camera: str, presets: dict[str, Any]) -> str:
    lines = [f"# Suggested for camera {camera}; merge by hand. One block per preset.", "cameras:", f"  {camera}:"]
    for name, e in presets.items():
        lines.append(f"    # at preset {name}")
        for zone, body in e["zones"].items():
            lines += [f"    zones:  # {name}", f"      {zone}:", f"        coordinates: {body['coordinates']}"]
        if e["motion_masks"]:
            lines += ["    motion:", "      mask:", *(f"        - {m}" for m in e["motion_masks"])]
    return "\n".join(lines) + "\n"
