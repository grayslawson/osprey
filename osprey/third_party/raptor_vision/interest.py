"""How much a person cares about a region, 0..1."""

from __future__ import annotations

from typing import Final

from .labels import UNKNOWN, canonical

# Interest of each label before the model or the NVR say anything. Door .. glare_source are Osprey's numbers;
# sidewalk, porch, timestamp and neighbour are new with the shared vocabulary (sidewalk like street, porch like path).
PRIOR: Final[dict[str, float]] = {
    "door": 1.0, "gate": 1.0, "driveway": 0.8, "path": 0.7, "porch": 0.7, "street": 0.5, "sidewalk": 0.5,
    "fence_line": 0.45, "yard": 0.35, "other": 0.15, "foliage": 0.12, "building": 0.1, "neighbour": 0.1,
    "sky": 0.0, "glare_source": 0.0, "timestamp": 0.0, UNKNOWN: 0.1,
}


def interest(label: str, entry: float, heat: float | None, model: bool) -> float:
    """0..1. 0.45 prior, 0.25 the model's entry sense, 0.30 heat; absent terms drop out.

    ``heat`` is None or a 0..1 number the caller computes (event density); this library does not know where it comes from.
    """
    parts = [(0.45, PRIOR.get(canonical(label), PRIOR["other"]))]
    if model:
        parts.append((0.25, entry))
    if heat is not None:
        parts.append((0.30, heat))
    total = sum(w for w, _ in parts)
    return round(sum(w * v for w, v in parts) / total, 4)
