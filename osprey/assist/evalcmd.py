"""``osprey assist eval`` and ``replay``: score a task against labelled frames.

This is what a model bake-off runs. Point it at a folder of frames and a ``cases.json``, pick the
task, the mode and the provider and model, and it reports accuracy next to the rules baseline,
how often the model's answer was usable, latency and cost. ``replay`` builds the cases from your
own approved and rejected decisions instead.

``cases.json`` is a list. For ``quality.diagnose``::

    {"id": "back-porch-1", "file": "a.jpg", "scene": "NIGHT", "preset": "BackPorch", "label": "glare"}

``label`` is one of glare, blur, under, over, noise or ok. For ``quality.judge``::

    {"id": "t1", "before": "b.jpg", "after": "a.jpg", "problem": "glare", "label": "after"}

where ``label`` is before, after or same.
"""

from __future__ import annotations

import json
import statistics
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import metrics as metrics_mod
from .config import AssistConfig
from .decide import Decider, DiagnoseContext, JudgeContext, TaskConfig
from .detect import Baselines, Detector
from .levers import build_levers, candidates, ranges
from .providers import build_provider
from .store import Store
from .types import Action, Diagnosis, Metrics


def _decider(cfg: AssistConfig, task: str, overrides: dict[str, Any], calls: list[dict[str, Any]]) -> tuple[Decider, TaskConfig]:
    base = cfg.tasks[task]
    fields = {k: v for k, v in overrides.items() if v is not None}
    chosen = replace(base, **fields)
    if chosen.mode != "rules" and (not chosen.provider or not chosen.model or chosen.provider not in cfg.providers):
        raise ValueError("a model mode needs --provider (one defined in the config) and --model")
    providers = {name: build_provider(name, p) for name, p in cfg.providers.items()}
    return Decider({**cfg.tasks, task: chosen}, providers, calls.append), chosen


def _stats(calls: list[dict[str, Any]], labels: list[tuple[str, str, str]], rules: list[str]) -> dict[str, Any]:
    n = len(labels)
    right = sum(1 for want, got, _ in labels if want == got)
    rules_right = sum(1 for (want, _, _), r in zip(labels, rules, strict=True) if want == r)
    ms = sorted(float(c["ms"]) for c in calls if c.get("ms") is not None)
    confusion: dict[str, dict[str, int]] = {}
    for want, got, _ in labels:
        confusion.setdefault(want, {}).setdefault(got, 0)
        confusion[want][got] += 1
    return {
        "cases": n,
        "accuracy": round(right / n, 4) if n else None,
        "rules_accuracy": round(rules_right / n, 4) if n else None,
        "confusion": confusion,
        "model_calls": len(calls),
        "usable_rate": round(sum(1 for c in calls if c.get("ok")) / len(calls), 4) if calls else None,
        "errors": sorted({str(c["error"]) for c in calls if c.get("error")})[:10],
        "latency_ms": {"p50": round(statistics.median(ms), 1), "p95": round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 1)}
        if ms else None,
        "cost": round(sum(float(c["cost"]) for c in calls if c.get("cost") is not None), 6) if calls else None,
        "tokens_in": sum(int(c["tokens_in"]) for c in calls if c.get("tokens_in")),
    }


def run_eval(cfg: AssistConfig, cases_dir: str | Path, task: str = "quality.diagnose",
             overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(cases_dir)
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
    calls: list[dict[str, Any]] = []
    decider, chosen = _decider(cfg, task, overrides or {}, calls)
    levers = build_levers(list(cfg.quality.levers), cfg.quality.limits)
    results: list[tuple[str, str, str]] = []
    rules_answers: list[str] = []
    detail: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        book = Baselines(Store(tmp))
        for case in cases:
            if task == "quality.diagnose":
                got, rule = _diagnose_case(cfg, decider, book, levers, root, case)
            else:
                got, rule = _judge_case(decider, root, case)
            results.append((case["label"], got, str(case.get("id", ""))))
            rules_answers.append(rule)
            detail.append({"id": case.get("id"), "label": case["label"], "answer": got, "rules": rule})
    out = _stats(calls, results, rules_answers)
    out.update({"task": task, "mode": chosen.mode, "provider": chosen.provider, "model": chosen.model,
                "send_frames": chosen.send_frames, "detail": detail})
    return out


def _diagnose_case(cfg: AssistConfig, decider: Decider, book: Baselines, levers: Any, root: Path,
                   case: dict[str, Any]) -> tuple[str, str]:
    frame = (root / case["file"]).read_bytes()
    scene = str(case.get("scene", "DAY")).upper()
    metrics = metrics_mod.compute(frame)
    z = book.z("eval", str(case.get("preset", "eval")), scene, 12, metrics)
    detector = Detector()
    findings = []
    for _ in range(2):  # the two-sample rule: a still frame is the same sample twice
        findings = detector.evaluate(metrics, z, problems=cfg.quality.problems, fire=cfg.quality.fire_thresholds)
    cands: dict[str, tuple[Action, ...]] = {p: candidates(p, scene, levers) for p in cfg.quality.problems}
    rules = Diagnosis(findings[0].problem, findings[0].confidence, "rules", actions=cands[findings[0].problem][:1]) \
        if findings else None
    ctx = DiagnoseContext("eval", str(case.get("preset", "eval")), scene, "12:00", metrics, z, tuple(findings), cands,
                          ranges(levers))
    final = decider.diagnose(ctx, rules, frame)
    answer = "ok" if final.problem in ("unknown", "ok") else final.problem
    return answer, rules.problem if rules else "ok"


def _judge_case(decider: Decider, root: Path, case: dict[str, Any]) -> tuple[str, str]:
    before, after = (root / case["before"]).read_bytes(), (root / case["after"]).read_bytes()
    mb, ma = metrics_mod.compute(before), metrics_mod.compute(after)
    problem = str(case.get("problem", "glare"))
    from .levers import TARGET  # local: only the judge needs it
    target, lower = TARGET[problem]
    rules = _rules_verdict(mb, ma, target, lower)
    verdict = decider.judge(JudgeContext(str(case.get("preset", "eval")), str(case.get("scene", "DAY")).upper(), problem,
                                         target, mb, ma), before, after, rules)
    return verdict.better, rules.better


def _rules_verdict(before: Metrics, after: Metrics, target: str, lower: bool) -> Any:
    from .types import Verdict
    b, a = float(getattr(before, target)), float(getattr(after, target))
    gain = ((b - a) if lower else (a - b)) / max(abs(b), 1e-3)
    better = "after" if gain > 0.1 else "before" if gain < -0.1 else "same"
    return Verdict(better, min(0.9, abs(gain)), "rules")


def cases_from_store(store: Store, into: Path) -> int:
    """Write ``cases.json`` and the frames from decisions a person judged. Returns the number of cases."""
    into.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    for row in store.decisions(limit=500):
        frame = store.frame(row["before_frame"]) if row.get("before_frame") else None
        latest = store.last_feedback(row["id"])
        if frame is None or latest is None:
            continue
        if row["status"] in ("applied", "kept"):
            label = row["problem"]
        elif latest["reason"] == "not_a_problem":
            label = "ok"
        else:
            continue
        name = f"d{row['id']}.jpg"
        (into / name).write_bytes(frame)
        cases.append({"id": f"d{row['id']}", "file": name, "scene": row["scene"], "preset": row["preset"], "label": label})
    (into / "cases.json").write_text(json.dumps(cases, indent=1), encoding="utf-8")
    return len(cases)
