"""Plain data shared by every Assist module. No I/O here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

PROBLEMS: Final = ("glare", "blur", "under", "over", "noise")
DENY_LEVERS: Final = frozenset({"focusPosition", "irLedLevel", "irLedMode", "wdr"})
MODES: Final = ("off", "observe", "suggest", "act", "auto")
GRID_W: Final = 12
GRID_H: Final = 8


@dataclass(frozen=True)
class Metrics:
    """Cheap image-quality numbers from one snapshot."""

    sharp: float          # median Laplacian variance of the sharpest half of tiles
    clip_hi: float        # fraction of pixels with luma >= 250
    clip_lo: float        # fraction of pixels with luma <= 5
    luma: float           # mean luma 0..255
    contrast: float       # p95 - p5 of luma
    noise: float          # high-pass stddev in the flattest tiles
    glare: float          # frame fraction of the largest hot tile cluster
    glare_cx: float | None  # centroid of that cluster, 0..1 left to right
    glare_cy: float | None  # 0..1 top to bottom
    glare_map: tuple[int, ...] = field(default=())  # GRID_W*GRID_H cells, 0-9 hot-pixel level, row major

    def as_dict(self) -> dict[str, object]:
        return {
            "sharp": self.sharp, "clip_hi": self.clip_hi, "clip_lo": self.clip_lo,
            "luma": self.luma, "contrast": self.contrast, "noise": self.noise,
            "glare": self.glare, "glare_cx": self.glare_cx, "glare_cy": self.glare_cy,
            "glare_map": list(self.glare_map),
        }


@dataclass(frozen=True)
class Finding:
    """One problem the detector believes it sees."""

    problem: str          # one of PROBLEMS
    confidence: float     # 0..1
    value: float          # the metric that tripped it
    z: float              # distance from this preset's baseline, in standard deviations


@dataclass(frozen=True)
class Action:
    """One change. ``lever`` is a name from levers.LEVERS (or night_view)."""

    lever: str
    change: float = 0.0       # signed step (isp levers) or offset fraction
    x: float | None = None    # touch_focus point, 0..1
    y: float | None = None


@dataclass(frozen=True)
class Diagnosis:
    """What a task (rules, a model, or both) concluded about one frame."""

    problem: str              # a problem, "ok" or "unknown"
    confidence: float
    source: str               # "rules", "model:<name>" or "both"
    evidence: str = ""
    actions: tuple[Action, ...] = ()
    ask_human: bool = False
    disagree: bool = False    # rules and model named different problems


@dataclass(frozen=True)
class Verdict:
    """quality.judge: did the change help?"""

    better: str               # "before", "after" or "same"
    confidence: float
    source: str
    reason: str = ""
