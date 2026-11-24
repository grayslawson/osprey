"""The one label vocabulary and the sentence that describes each label to a model.

Seventeen labels. Each adapter passes the subset it uses with ``labels=``. ``building`` and ``wall_eaves`` name the same
thing (Kestrel said building, Osprey said wall_eaves): ``wall_eaves`` is an alias of ``building``. Both stay accepted so
neither product has to rename a stored label; ask a model for one of them, never both (see ``canonical``).
"""

from __future__ import annotations

from typing import Final

LABELS: Final = (
    "door", "gate", "driveway", "path", "street", "sidewalk", "yard", "porch", "fence_line", "sky",
    "wall_eaves", "building", "foliage", "glare_source", "timestamp", "neighbour", "other",
)
UNKNOWN: Final = "unknown"  # not a label a model may answer with: "no label found"
ALIASES: Final = {"wall_eaves": "building"}

_BUILDING = "walls, roof edge, eaves, gutters or the ceiling of a porch, of the owner's buildings"
CRITERIA: Final[dict[str, str]] = {
    "door": "a building entrance door: front, back, side or garage man-door",
    "gate": "a gate in a fence or wall, or a garage door, that people or vehicles pass through",
    "driveway": "a driveway, parking area or a place a vehicle stops at the property",
    "path": "a footpath, walkway or steps on the property that people walk on",
    "street": "a public road where passing vehicles drive, or the street beyond the property",
    "sidewalk": "a public footpath along a road",
    "yard": "lawn, garden, patio or open ground inside the property",
    "porch": "a porch, deck or the landing in front of a door",
    "fence_line": "a fence, hedge line or property boundary",
    "sky": "sky, clouds or nothing but distant air",
    "wall_eaves": _BUILDING,
    "building": _BUILDING,
    "foliage": "trees, bushes or plants that move in the wind",
    "glare_source": "a light, reflection or bright wash that hides the scene",
    "timestamp": "a burned-in clock, name or logo overlay text",
    "neighbour": "a neighbouring property, windows or yard that is not the owner's",
    "other": "something else, or it cannot be told",
}
COLORS: Final[dict[str, tuple[int, int, int]]] = {
    "door": (230, 80, 60), "gate": (240, 150, 40), "driveway": (120, 130, 150), "path": (200, 170, 110),
    "street": (80, 80, 100), "sidewalk": (170, 170, 190), "yard": (110, 190, 90), "porch": (210, 140, 120),
    "fence_line": (190, 120, 200), "sky": (90, 170, 240), "wall_eaves": (160, 160, 150), "building": (160, 160, 150),
    "foliage": (30, 130, 60), "glare_source": (255, 240, 80), "timestamp": (255, 255, 255),
    "neighbour": (120, 100, 90), "other": (150, 150, 150), UNKNOWN: (0, 0, 0),
}


def canonical(label: str) -> str:
    """The name to compare labels by: ``wall_eaves`` becomes ``building``."""
    return ALIASES.get(label, label)


def check_labels(labels: tuple[str, ...] | list[str]) -> None:
    """A subset must be known labels and must not hold an alias together with its target."""
    if bad := [k for k in labels if k not in CRITERIA]:
        raise ValueError(f"unknown label(s) {bad}")
    names = [canonical(k) for k in labels]
    if len(set(names)) != len(names):
        raise ValueError("building and wall_eaves are one label: pass only one of them")
