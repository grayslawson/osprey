"""Baselines per preset, day or night and hour, and the rules that turn metrics into findings.

A baseline is a running mean and variance. It learns only from healthy samples, so a
fault never becomes normal. Until a bucket has ``MIN_N`` samples the hour is ignored
(scene only), and until the scene bucket has them too the check falls back to fixed priors.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Final

from .store import Store
from .types import Finding, Metrics

MIN_N: Final = 8        # samples before a bucket is trusted
HOUR_MIN_N: Final = 20  # samples before the hour-of-day bucket beats the scene bucket
EWMA_AFTER: Final = 40  # past this many samples the mean follows recent values
ALPHA: Final = 0.05
WATCHED: Final = ("sharp", "clip_hi", "clip_lo", "luma", "contrast", "noise", "glare")
# Fallback (mean, std) before any baseline exists. Glare, clipping and luma have sane absolute priors;
# sharp and noise do not, so blur and noise cannot fire until a baseline is learned.
_PRIORS: Final[dict[str, dict[str, tuple[float, float]]]] = {
    "DAY": {"luma": (110, 40), "glare": (0.0, 0.03), "clip_hi": (0.01, 0.01), "clip_lo": (0.01, 0.01)},
    "NIGHT": {"luma": (70, 35), "glare": (0.0, 0.03), "clip_hi": (0.01, 0.01), "clip_lo": (0.05, 0.05)},
}
_FLOOR: Final = {"sharp": (0.1, 1.0), "luma": (0.0, 3.0), "glare": (0.0, 0.01), "clip_hi": (0.0, 0.005),
                 "clip_lo": (0.0, 0.005), "noise": (0.1, 0.2), "contrast": (0.05, 3.0)}


def _std(metric: str, mean: float, var: float) -> float:
    rel, absolute = _FLOOR.get(metric, (0.0, 1e-6))
    return max(math.sqrt(max(var, 0.0)), abs(mean) * rel, absolute)


class Baselines:
    def __init__(self, store: Store) -> None:
        self.store = store

    def z(self, camera: str, preset: str, scene: str, hour: int, metrics: Metrics) -> dict[str, float]:
        out: dict[str, float] = {}
        for name in WATCHED:
            value = float(getattr(metrics, name))
            row = self.store.baseline(camera, preset, scene, hour, name)
            if row is None or row[0] < HOUR_MIN_N:
                row = self.store.baseline(camera, preset, scene, -1, name) or row
            if row is not None and row[0] >= MIN_N:
                out[name] = (value - row[1]) / _std(name, row[1], row[2])
            elif name in _PRIORS.get(scene, _PRIORS["DAY"]):
                mean, std = _PRIORS.get(scene, _PRIORS["DAY"])[name]
                out[name] = (value - mean) / std
            else:
                out[name] = 0.0
        return out

    def learn(self, camera: str, preset: str, scene: str, hour: int, metrics: Metrics) -> None:
        """Fold a healthy sample into the hour bucket and the scene bucket."""
        for name in WATCHED:
            value = float(getattr(metrics, name))
            for bucket in (hour, -1):
                n, mean, var = self.store.baseline(camera, preset, scene, bucket, name) or (0, value, 0.0)
                n += 1
                if n <= EWMA_AFTER:
                    delta = value - mean
                    mean += delta / n
                    var += (delta * (value - mean) - var) / n
                else:
                    diff = value - mean
                    mean += ALPHA * diff
                    var = (1 - ALPHA) * (var + ALPHA * diff * diff)
                self.store.set_baseline(camera, preset, scene, bucket, name, n, mean, max(var, 0.0))


# (fire z, clear z): a problem fires above the first and clears below the second.
THRESHOLDS: Final[dict[str, tuple[float, float]]] = {
    "blur": (2.5, 1.5), "glare": (2.0, 1.0), "under": (2.0, 1.0), "over": (2.0, 1.0), "noise": (2.5, 1.5),
}


def _confidence(z: float, threshold: float) -> float:
    return round(min(0.9, 1 / (1 + math.exp(-(abs(z) - threshold + 1.5)))), 3)


@dataclass
class Detector:
    """Per (camera, preset, scene) memory for the two-sample rule and the hysteresis."""

    streak: dict[str, int] = field(default_factory=dict)
    active: set[str] = field(default_factory=set)

    def evaluate(self, metrics: Metrics, z: dict[str, float], *, problems: tuple[str, ...],
                 scale: dict[str, float] | None = None, fire: dict[str, float] | None = None) -> list[Finding]:
        scale = scale or {}
        fire = fire or {}
        raw: dict[str, Finding | None] = {}
        thr = {p: (fire.get(p, THRESHOLDS[p][0]) * scale.get(p, 1.0), THRESHOLDS[p][1] * scale.get(p, 1.0))
               for p in THRESHOLDS}
        raw["blur"] = self._hit("blur", z["sharp"] < -thr["blur"][0], metrics.sharp, z["sharp"], thr["blur"][0]) \
            if metrics.clip_hi < 0.2 else None
        raw["glare"] = self._hit("glare", metrics.glare > 0.08 and z["glare"] > thr["glare"][0], metrics.glare,
                                 z["glare"], thr["glare"][0])
        raw["under"] = self._hit("under", z["luma"] < -thr["under"][0] and metrics.clip_lo > 0.15, metrics.luma,
                                 z["luma"], thr["under"][0])
        raw["over"] = self._hit("over", z["luma"] > thr["over"][0] and metrics.clip_hi > 0.05, metrics.luma,
                                z["luma"], thr["over"][0])
        raw["noise"] = self._hit("noise", z["noise"] > thr["noise"][0], metrics.noise, z["noise"], thr["noise"][0])
        out: list[Finding] = []
        for problem in problems:
            hit = raw.get(problem)
            if hit is not None:
                self.streak[problem] = self.streak.get(problem, 0) + 1
                if self.streak[problem] >= 2 or problem in self.active:
                    self.active.add(problem)
                    out.append(hit)
                continue
            self.streak[problem] = 0
            if problem in self.active and self._cleared(problem, metrics, z, thr[problem][1]):
                self.active.discard(problem)
            elif problem in self.active:  # in the grey band: still a problem, no fresh evidence
                out.append(Finding(problem, 0.5, 0.0, 0.0))
        return sorted(out, key=lambda f: -f.confidence)

    @staticmethod
    def _hit(problem: str, fired: bool, value: float, z: float, threshold: float) -> Finding | None:
        return Finding(problem, _confidence(z, threshold), round(value, 4), round(z, 2)) if fired else None

    @staticmethod
    def _cleared(problem: str, metrics: Metrics, z: dict[str, float], clear: float) -> bool:
        key = {"blur": "sharp", "glare": "glare", "under": "luma", "over": "luma", "noise": "noise"}[problem]
        value = -z[key] if problem in ("blur", "under") else z[key]
        return value < clear


def local_hour(clock: float | None = None) -> int:
    return time.localtime(clock if clock is not None else time.time()).tm_hour
