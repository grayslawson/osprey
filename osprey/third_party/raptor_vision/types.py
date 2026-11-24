"""Frame and Region: the two types every stage shares. No NVR, PTZ or product names here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Point = tuple[float, float]
Poly = list[Point]
Kind = Literal["day", "night"]


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    width: int
    height: int
    kind: Kind
    ts: float  # unix seconds the picture was taken
    key: str = ""  # opaque to this library: Osprey puts a cell here, Kestrel leaves it empty


@dataclass
class Region:
    label: str
    polygon: Poly  # relative coordinates, 0..1
    confidence: float
    source: str  # "chat:<model>", "decider:<model>", "rules", "metrics" or "segmenter"
    refined: bool = False
    note: str = ""
    extra: dict[str, object] = field(default_factory=dict)  # may hold "entry" and "closer", both 0..1
