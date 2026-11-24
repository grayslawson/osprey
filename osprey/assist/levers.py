"""The levers Assist may pull, their limits, and the ladder of fixes for each problem.

ISP values have no read-back on the camera, so every number here is a limit on what
Assist will *write*, not a statement of what the camera reports. The defaults are
cautious. ``gain`` and ``shutter`` are off until a config gives them explicit limits,
because their units differ between models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .types import DENY_LEVERS, Action


@dataclass(frozen=True)
class Lever:
    name: str
    kind: str          # "isp" (ChangeIspSettings), "move" (a small PTZ move) or "structural"
    key: str           # the ISP key, or the name for moves
    lo: float
    hi: float
    step: float
    default: float     # shortcut: assumed current value when Assist has never written this key

    def clamp(self, value: float) -> float:
        return min(self.hi, max(self.lo, value))


# lo/hi/step are write limits. touch_focus is opt-in until its units are confirmed on hardware.
DEFAULT_LEVERS: Final[dict[str, Lever]] = {
    "aeTargetPercent": Lever("aeTargetPercent", "isp", "aeTargetPercent", 20, 80, 5, 50),
    "brightness": Lever("brightness", "isp", "brightness", 20, 80, 5, 50),
    "contrast": Lever("contrast", "isp", "contrast", 20, 80, 5, 50),
    "sharpness": Lever("sharpness", "isp", "sharpness", 0, 100, 5, 50),
    "denoise": Lever("denoise", "isp", "denoise", 0, 100, 10, 50),
    "hdr": Lever("hdr", "isp", "hdr", 0, 3, 1, 1),
    "zoom_nudge": Lever("zoom_nudge", "move", "zoom_nudge", 0.005, 0.02, 0.005, 0.01),
    "night_view": Lever("night_view", "structural", "night_view", 0.0, 0.04, 0.01, 0.0),
    "icrSensitivity": Lever("icrSensitivity", "structural", "icrSensitivity", 0, 100, 10, 50),
}
OPTIONAL_LEVERS: Final = ("touch_focus", "gain", "shutter")
# Written lever names that are never allowed, whoever asks.
assert not DENY_LEVERS & set(DEFAULT_LEVERS)

# Ladders: what to try first for each problem. A sign gives the direction; the step comes from the lever.
_LADDERS: Final[dict[str, dict[str, tuple[tuple[str, int], ...]]]] = {
    "glare": {"NIGHT": (("night_view", 1), ("aeTargetPercent", -1), ("hdr", 1)),
              "DAY": (("aeTargetPercent", -1), ("hdr", 1))},
    "blur": {"NIGHT": (("zoom_nudge", 1), ("touch_focus", 1), ("sharpness", 1), ("denoise", -1)),
             "DAY": (("zoom_nudge", 1), ("touch_focus", 1), ("sharpness", 1))},
    "under": {"NIGHT": (("aeTargetPercent", 1), ("brightness", 1)), "DAY": (("aeTargetPercent", 1), ("brightness", 1))},
    "over": {"NIGHT": (("aeTargetPercent", -1), ("contrast", -1)), "DAY": (("aeTargetPercent", -1), ("contrast", -1))},
    "noise": {"NIGHT": (("denoise", 1), ("aeTargetPercent", -1)), "DAY": (("denoise", 1),)},
}
# The metric each problem is judged on, and whether a lower value is better.
TARGET: Final[dict[str, tuple[str, bool]]] = {
    "glare": ("glare", True), "blur": ("sharp", False), "under": ("luma", False),
    "over": ("luma", True), "noise": ("noise", True),
}
# A fix must not make these worse.
GUARDS: Final[dict[str, tuple[str, ...]]] = {
    "glare": ("clip_lo",), "blur": ("noise",), "under": ("noise", "clip_hi"),
    "over": ("clip_lo",), "noise": ("luma",),
}


def build_levers(enabled: list[str], limits: dict[str, dict[str, float]]) -> dict[str, Lever]:
    """The levers this install may use: the enabled names, with any limit overrides applied."""
    out: dict[str, Lever] = {}
    for name in enabled:
        if name in DENY_LEVERS:
            raise ValueError(f"{name} is ignored by the camera and may not be used")
        base = DEFAULT_LEVERS.get(name)
        given = limits.get(name)
        if base is None:
            if given is None or set(given) != {"lo", "hi", "step", "default"}:
                raise ValueError(f"lever {name} needs limits with lo, hi, step and default")
            kind = "move" if name == "touch_focus" else "isp"
            base = Lever(name, kind, name, given["lo"], given["hi"], given["step"], given["default"])
        elif given:
            base = Lever(base.name, base.kind, base.key, given.get("lo", base.lo), given.get("hi", base.hi),
                         given.get("step", base.step), given.get("default", base.default))
        if not base.lo <= base.default <= base.hi or base.step <= 0:
            raise ValueError(f"lever {name} has inconsistent limits")
        out[name] = base
    return out


def candidates(problem: str, scene: str, levers: dict[str, Lever],
               blocked: frozenset[str] = frozenset()) -> tuple[Action, ...]:
    """The ordered fixes worth trying for ``problem``, skipping levers that are off or cooling down."""
    rungs = _LADDERS.get(problem, {}).get(scene if scene in ("DAY", "NIGHT") else "DAY", ())
    out: list[Action] = []
    for name, sign in rungs:
        lever = levers.get(name)
        if lever is None or name in blocked:
            continue
        if name == "touch_focus":
            out.append(Action(name, sign * lever.step, 0.5, 0.5))  # the engine aims it at the sharpest-loss tile
        else:
            out.append(Action(name, sign * lever.step))
    return tuple(out)


def next_value(lever: Lever, shadow: dict[str, float], change: float) -> float | None:
    """The value to write, or None when the lever is already at its limit in that direction."""
    current = shadow.get(lever.key, lever.default)
    wanted = lever.clamp(round((current + change) / lever.step) * lever.step if lever.kind == "isp" else current + change)
    return None if wanted == current else wanted


def ranges(levers: dict[str, Lever]) -> dict[str, tuple[float, float, float]]:
    return {name: (lv.lo, lv.hi, lv.step) for name, lv in levers.items()}
