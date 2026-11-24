"""Per-signal decision models (glare, blur, action, judge) in the closed loop, against a stub gateway."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import assist.config as acfg
from assist.decide import parse_task
from assist.providers import build_provider
from assist.signals import ACTION_LEVER, QUESTIONS, Signals
from stub_models import HTTP_500, StubModels, decisions_body
from test_assist_loop import Rig, goto


def answers(glare: float, blur: float, action: str = "night_view") -> dict[str, Any]:
    return decisions_body({
        "glare": {"type": "noul", "noul": glare}, "blur": {"type": "noul", "noul": blur},
        "followup": {"type": "noul", "noul": 0.1},
        "exposure": {"type": "choice", "choice": "ok", "probabilities": {"ok": 1.0}},
        "action": {"type": "choice", "choice": action, "probabilities": {action: 0.9}},
    })


def tasks(**over: Any) -> dict[str, Any]:
    base = {"provider": "gw", "send_frames": True}
    return {
        "quality.glare": {**base, "mode": "model", "model": "jev", "threshold": 0.57,
                          "fallback": [{"model": "pplx", "threshold": 0.37}], **over.get("glare", {})},
        "quality.blur": {**base, "mode": "model", "model": "flash", "threshold": 0.6, **over.get("blur", {})},
        "quality.action": {**base, "mode": "model", "model": "clef", **over.get("action", {})},
    }


def rig_with(tmp: Path, stub: StubModels, *, allow_frames: bool = True, mode: str = "suggest", world: dict[str, Any] | None = None,
             **task_over: Any) -> Rig:
    providers = {"gw": {"kind": "decisions", "base_url": stub.url, "allow_frames": allow_frames}}
    rig = Rig(tmp, mode=mode, world=world, providers=providers, tasks=tasks(**task_over))
    rig.signals = Signals(rig.cfg.tasks, {n: build_provider(n, p) for n, p in rig.cfg.providers.items()}, rig.store.log_call)  # type: ignore[attr-defined]
    return rig


def detect(rig: Rig) -> int | None:
    goto(rig, "BackPorch")
    rig.warm()  # the baseline is learned on the rules alone; the models join afterwards
    rig.engine.signals = rig.signals  # type: ignore[attr-defined]
    return next(iter(i for i in [rig.engine.check("default", "arrival") for _ in range(2)] if i), None)


def test_a_glare_model_over_its_threshold_proposes_the_night_view(tmp_path: Path) -> None:
    with StubModels(decisions_reply=answers(0.9, 0.05)) as stub:
        rig = rig_with(tmp_path, stub)
        try:
            row = rig.store.decision(detect(rig) or 0)
            assert row is not None and row["problem"] == "glare" and row["source"] == "model:jev"
            assert row["confidence"] > 0.5 and "jev" not in row["change"] and row["change"].startswith("New night view")
            body = stub.requests[0]["json"]
            assert set(body["questions"]) == set(QUESTIONS)  # the wording the thresholds were tuned with
            assert any(isinstance(x, dict) and x.get("type") == "image_url" for x in body["state"])
        finally:
            rig.close()


def test_a_model_below_its_threshold_overrules_the_rules_guess(tmp_path: Path) -> None:
    with StubModels(decisions_reply=answers(0.2, 0.05)) as stub:
        rig = rig_with(tmp_path, stub)
        try:
            assert detect(rig) is None  # the glare rule fires on this frame, the model says no
            assert rig.store.decisions() == []
        finally:
            rig.close()


def test_the_fallback_model_answers_when_the_first_one_fails(tmp_path: Path) -> None:
    with StubModels(decisions_reply=[HTTP_500, answers(0.5, 0.05)]) as stub:  # 0.5 beats pplx's 0.37, not jev's 0.57
        rig = rig_with(tmp_path, stub)
        try:
            row = rig.store.decision(detect(rig) or 0)
            assert row is not None and row["problem"] == "glare"
            assert [r["json"]["model"] for r in stub.requests[:2]] == ["jev", "pplx"]
            assert rig.store._run("SELECT COUNT(*) AS c FROM model_calls WHERE ok=0").fetchone()["c"] >= 1
        finally:
            rig.close()


def test_every_model_failing_leaves_the_rules_in_charge(tmp_path: Path) -> None:
    with StubModels(decisions_reply=HTTP_500) as stub:
        rig = rig_with(tmp_path, stub)
        try:
            row = rig.store.decision(detect(rig) or 0)
            assert row is not None and row["source"] == "rules"
        finally:
            rig.close()


def test_without_both_frame_opt_ins_the_picture_never_leaves(tmp_path: Path) -> None:
    with StubModels(decisions_reply=answers(0.9, 0.05)) as stub:
        for allow, send in ((False, True), (True, False)):
            (tmp_path / f"{allow}{send}").mkdir()
            rig = rig_with(tmp_path / f"{allow}{send}", stub, allow_frames=allow,
                           glare={"send_frames": send}, blur={"send_frames": send}, action={"send_frames": send})
            try:
                row = rig.store.decision(detect(rig) or 0)
                assert row is not None and row["source"] == "rules"
            finally:
                rig.close()
        assert stub.requests == []


def test_an_action_of_none_means_no_change_is_proposed(tmp_path: Path) -> None:
    with StubModels(decisions_reply=answers(0.9, 0.05, action="none")) as stub:
        rig = rig_with(tmp_path, stub)
        try:
            row = rig.store.decision(detect(rig) or 0)
            assert row is not None and row["status"] == "observed" and rig.world.isp_log == []
        finally:
            rig.close()


def test_the_action_model_picks_among_the_ladder_rungs(tmp_path: Path) -> None:
    with StubModels(decisions_reply=answers(0.9, 0.05, action="aeTargetPercent_down")) as stub:
        rig = rig_with(tmp_path, stub, world={"night": False, "glare": None, "exposure": 1.5})
        try:
            row = rig.store.decision(detect(rig) or 0)
            assert row is not None and row["problem"] == "glare" and row["change"].startswith("aeTargetPercent 50 to 45")
        finally:
            rig.close()


def test_the_judge_compares_the_same_models_probability_before_and_after(tmp_path: Path) -> None:
    replies: list[Any] = [answers(0.9, 0.0), answers(0.2, 0.0), answers(0.2, 0.0), answers(0.9, 0.0), answers(0.5, 0.0), answers(0.5, 0.0)]
    with StubModels(decisions_reply=replies) as stub:
        rig = rig_with(tmp_path, stub)
        try:
            frame = rig.world.render()
            sig = rig.signals  # type: ignore[attr-defined]
            assert sig.judge("glare", frame, frame)[0] == "after"    # 0.9 -> 0.2
            assert sig.judge("glare", frame, frame)[0] == "before"   # 0.2 -> 0.9
            assert sig.judge("glare", frame, frame)[0] == "same"
            assert sig.judge("noise", frame, frame) is None           # no model signal for this problem
        finally:
            rig.close()


def test_task_config_checks_thresholds_and_fallbacks() -> None:
    ok = parse_task("quality.glare", {"mode": "model", "provider": "gw", "model": "m", "threshold": 0.57,
                                      "fallback": [{"model": "n", "threshold": 0.37}, {"model": "o"}]})
    assert ok.threshold == 0.57 and ok.fallback == (("n", 0.37), ("o", None))
    for bad in ({"threshold": 2}, {"threshold": True}, {"fallback": [{"threshold": 0.1}]}, {"fallback": [{"model": "x", "extra": 1}]},
                {"fallback": ["x"]}):
        with pytest.raises(ValueError):
            parse_task("quality.glare", {"mode": "model", "provider": "gw", "model": "m", **bad})
    assert acfg.parse({"providers": {"gw": {"kind": "decisions", "base_url": "http://x"}}, "tasks": tasks()}).tasks["quality.glare"].threshold == 0.57


def test_every_action_word_maps_to_a_known_lever() -> None:
    assert set(ACTION_LEVER) == set(QUESTIONS["action"]["criteria"]) - {"none", "wait"}
