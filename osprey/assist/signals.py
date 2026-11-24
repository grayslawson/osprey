"""Per-signal decision models: is there glare, is it blurred, which fix, did it help.

A 2026-11-24 bake-off on real G5 frames found a different best model for each question, and that
a decision model beats the local rules for glare (F1 0.96 against 0.74) and blur (0.98 against
0.82). Each question is its own task (`quality.glare`, `quality.blur`, `quality.action`), with its
own model, threshold and fallbacks. Exposure stays with the rules: the decision models over-called
it on ordinary night infrared frames.

The wording below is the wording the thresholds were tuned with. Every request carries the full
question set, as in the bake-off, and only the answer that is needed is read. Change the text and the
thresholds no longer apply.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from .decide import TaskConfig
from .providers import DecisionsProvider, ProviderError

log = logging.getLogger("osprey.assist.signals")

_TEXT: Final = {
    "glare": ("Glare: a bright blown-out bloom, halo, lens flare, IR bounce, headlight bloom or veiling haze that covers "
              "a noticeable region of the picture and hides scene detail. A small point light (street lamp) alone is NOT glare."),
    "blur": ("Blur: the picture is out of focus, motion-smeared (camera moving), or soft from rain/fog on the dome, "
             "so fine detail is lost."),
    "followup": ("Followup: a person or animal is visible and small enough that a zoom-in would be useful. "
                 "Cars alone, tiny birds and empty scenes are false."),
}
QUESTIONS: Final[dict[str, Any]] = {
    "glare": {"type": "noul", "instructions": _TEXT["glare"]},
    "blur": {"type": "noul", "instructions": _TEXT["blur"]},
    "followup": {"type": "noul", "instructions": _TEXT["followup"]},
    "exposure": {"type": "choice", "instructions": "Judge the exposure of this camera frame.",
                 "criteria": {"ok": "Exposure fine; a normal night IR scene with lit road and dark lawn is ok.",
                              "under": "Too dark to see detail that should be visible.", "over": "Washed out or blown out."}},
    "action": {"type": "choice", "instructions": "Best single corrective action for this camera frame.",
               "criteria": {"none": "Image fine, or a transient like a passing headlight.",
                            "touch_focus": "Out of focus or soft: re-focus.",
                            "hdr": "Daytime sun glare or lens flare.",
                            "night_view": "Night IR bounce or house-side glare: choose a different view.",
                            "aeTargetPercent_up": "Under-exposed.", "aeTargetPercent_down": "Over-exposed.",
                            "wait": "Camera is moving (motion smear): recapture later."}},
}
STATE_TEXT: Final = "Frame from a PTZ security camera (unknown time)."
# The action model's words, mapped to Assist's levers. A re-focus is a small zoom nudge on this camera.
ACTION_LEVER: Final = {"touch_focus": ("zoom_nudge", 1), "hdr": ("hdr", 1), "night_view": ("night_view", 1),
                       "aeTargetPercent_up": ("aeTargetPercent", 1), "aeTargetPercent_down": ("aeTargetPercent", -1)}
JUDGE_MARGIN: Final = 0.10


@dataclass(frozen=True)
class Signal:
    positive: bool
    p: float
    model: str
    threshold: float
    confidence: float  # 0.5 at the threshold, 1.0 at certainty: a monotone stand-in the rest of Assist can gate on


def _confidence(p: float, threshold: float) -> float:
    if p <= threshold:
        return 0.0
    return round(0.5 + 0.5 * (p - threshold) / max(1e-6, 1.0 - threshold), 3)


class Signals:
    def __init__(self, tasks: dict[str, TaskConfig], providers: dict[str, Any],
                 log_call: Callable[[dict[str, Any]], None] = lambda rec: None) -> None:
        self.tasks, self.providers, self._log = tasks, providers, log_call

    def active(self, name: str) -> bool:
        t = self.tasks.get(name)
        return bool(t is not None and t.mode != "rules" and t.model and isinstance(self.providers.get(t.provider or ""), DecisionsProvider))

    def _chain(self, task: TaskConfig) -> list[tuple[str, float | None]]:
        return [(task.model or "", task.threshold), *task.fallback]

    def _ask(self, name: str, frame: bytes, key: str) -> tuple[Any, str, float | None] | None:
        """Walk the model chain. Returns (answer, model, threshold) from the first model that answers, else None."""
        task = self.tasks[name]
        provider = self.providers[task.provider or ""]
        if not (provider.cfg.allow_frames and task.send_frames):
            return None  # these models need the picture, and the picture may not leave without both opt-ins
        for model, threshold in self._chain(task):
            rec: dict[str, Any] = {"provider": provider.name, "model": model, "task": name, "ok": False, "error": None,
                                   "ms": None, "tokens_in": None, "tokens_out": None, "cost": None}
            try:
                reply = provider.decide(model, [STATE_TEXT, frame], QUESTIONS)
                rec.update(ms=reply.ms, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out, cost=reply.cost)
                answer = reply.data.get(key)
                if not isinstance(answer, dict) or answer.get("type") not in ("noul", "choice"):
                    raise ValueError("the model refused or skipped the question")
                rec["ok"] = True
                self._log(rec)
                return answer, model, threshold
            except (ProviderError, ValueError, TypeError) as exc:
                rec["error"] = str(exc)[:120]
                self._log(rec)
        return None

    def _signal(self, name: str, key: str, frame: bytes) -> Signal | None:
        if not self.active(name):
            return None
        got = self._ask(name, frame, key)
        if got is None:
            return None
        answer, model, threshold = got
        try:
            p = float(answer["noul"])
        except (KeyError, TypeError, ValueError):
            return None
        thr = 0.5 if threshold is None else float(threshold)
        return Signal(p >= thr, p, model, thr, _confidence(p, thr))

    def glare(self, frame: bytes) -> Signal | None:
        return self._signal("quality.glare", "glare", frame)

    def blur(self, frame: bytes) -> Signal | None:
        return self._signal("quality.blur", "blur", frame)

    def action(self, frame: bytes) -> tuple[str, int] | None | str:
        """The corrective action the model picks: (lever, direction), "none" or "wait". None when unavailable."""
        if not self.active("quality.action"):
            return None
        got = self._ask("quality.action", frame, "action")
        if got is None:
            return None
        choice = got[0].get("choice")
        if choice in ("none", "wait"):
            return str(choice)
        return ACTION_LEVER.get(str(choice))

    def judge(self, problem: str, before: bytes, after: bytes) -> tuple[str, float, str] | None:
        """Compare the same model's probability on the two frames. Only glare and blur have a model signal."""
        name = {"glare": "quality.glare", "blur": "quality.blur"}.get(problem)
        if name is None or not self.active(name):
            return None
        key = problem
        pb, pa = self._signal(name, key, before), self._signal(name, key, after)
        if pb is None or pa is None:
            return None
        if pa.p <= pb.p - JUDGE_MARGIN:
            return "after", round(min(1.0, pb.p - pa.p + 0.5), 2), f"{pa.model}: {key} {pb.p:.2f} to {pa.p:.2f}"
        if pa.p >= pb.p + JUDGE_MARGIN:
            return "before", round(min(1.0, pa.p - pb.p + 0.5), 2), f"{pa.model}: {key} {pb.p:.2f} to {pa.p:.2f}"
        return "same", 0.5, f"{pa.model}: {key} {pb.p:.2f} to {pa.p:.2f}"
