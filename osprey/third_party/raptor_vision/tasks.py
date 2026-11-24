"""Per-task model settings: rules, a model, or both."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

MODES: Final = ("rules", "model", "both")
_TASK_KEYS = {"mode", "provider", "model", "send_frames", "min_confidence", "threshold", "fallback"}


@dataclass(frozen=True)
class TaskConfig:
    mode: str = "rules"
    provider: str | None = None
    model: str | None = None
    send_frames: bool = False  # a task that sends pictures needs this AND a provider with allow_frames
    min_confidence: float = 0.7
    # Signal tasks: the model's yes probability must reach ``threshold``. Probabilities are not comparable across
    # models, so every model has its own threshold.
    threshold: float | None = None
    fallback: tuple[tuple[str, float | None], ...] = ()  # (model, threshold), tried in order when the first fails


def _unit(name: str, key: str, v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise ValueError(f"task {name}: {key} must be 0..1")
    return float(v)


def parse_task(name: str, raw: dict[str, Any]) -> TaskConfig:
    if not isinstance(raw, dict):
        raise ValueError(f"task {name}: must be an object")
    unknown = set(raw) - _TASK_KEYS
    if unknown:
        raise ValueError(f"task {name}: unknown key(s) {sorted(unknown)}")
    mode = raw.get("mode", "rules")
    if mode not in MODES:
        raise ValueError(f"task {name}: mode must be one of {MODES}")
    mc = _unit(name, "min_confidence", raw.get("min_confidence", 0.7))
    if not isinstance(raw.get("send_frames", False), bool):
        raise ValueError(f"task {name}: send_frames must be a boolean")
    for k in ("provider", "model"):
        v = raw.get(k)
        if v is not None and (not isinstance(v, str) or not v):
            raise ValueError(f"task {name}: {k} must be a non-empty string")
    if mode != "rules" and not (raw.get("provider") and raw.get("model")):
        raise ValueError(f"task {name}: mode {mode} needs provider and model")
    thr = raw.get("threshold")
    fallback: list[tuple[str, float | None]] = []
    for item in raw.get("fallback", []):
        if (not isinstance(item, dict) or set(item) - {"model", "threshold"} or not isinstance(item.get("model"), str)
                or not item["model"]):
            raise ValueError(f"task {name}: fallback entries are {{\"model\": ..., \"threshold\": ...}}")
        t = item.get("threshold")
        fallback.append((item["model"], None if t is None else _unit(name, "fallback threshold", t)))
    return TaskConfig(mode, raw.get("provider"), raw.get("model"), raw.get("send_frames", False), mc,
                      None if thr is None else _unit(name, "threshold", thr), tuple(fallback))
