"""Osprey Assist model layer: providers, per-task modes, validation and fallbacks."""

from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from assist.decide import Decider, DiagnoseContext, JudgeContext, TaskConfig, parse_task
from assist.providers import (
    Budget,
    ChatProvider,
    DecisionsProvider,
    ProviderError,
    build_provider,
    parse_provider,
)
from assist.types import Action, Diagnosis, Finding, Metrics, Verdict
from stub_models import (
    HTTP_429,
    HTTP_500,
    INVALID_JSON,
    MALFORMED,
    SLOW,
    StubModels,
    chat_body,
    decisions_body,
)

JPEG = b"\xff\xd8\xff-frame-bytes"
JPEG_B64 = base64.b64encode(JPEG).decode()
METRICS = Metrics(sharp=40.0, clip_hi=0.12, clip_lo=0.0, luma=150.0, contrast=200.0, noise=2.0,
                  glare=0.1, glare_cx=0.7, glare_cy=0.3, glare_map=tuple(range(10)) * 9 + (0,) * 6)
CTX = DiagnoseContext(
    camera="Front Door", preset="night", scene="porch", local_time="23:10", metrics=METRICS,
    z={"sharp": -0.5, "luma": 2.0, "noise": 0.2}, findings=(Finding("glare", 0.8, 0.1, 3.0),),
    candidates={"glare": (Action("exposure", -2.0), Action("gain", -1.0)), "blur": (Action("sharpness", 1.0),)},
    lever_ranges={"exposure": (-6, 6, 1), "gain": (0, 10, 2), "sharpness": (0, 10, 1)},
    recent=("10:00 exposure -1",), similar=("glare then exposure -2: better",),
)
RULES = Diagnosis("glare", 0.8, "rules", "rules say glare", (Action("exposure", -2.0),))
JCTX = JudgeContext("night", "porch", "glare", "glare", METRICS, dataclasses.replace(METRICS, glare=0.01))
GOOD_CHAT = {"problem": "glare", "confidence": 0.9, "evidence": "hot spot top right",
             "actions": [{"lever": "exposure", "change": -2, "x": None, "y": None}], "ask_human": False}


def choice(opt: str, p: float, others: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "choice", "choice": opt, "probabilities": {opt: p, **{o: (1 - p) / max(1, len(others)) for o in others}}}


GOOD_DEC = {"problem": choice("glare", 0.9, ("ok",)), "lever": choice("exposure", 0.8), "fix_needed": {"type": "noul", "noul": 0.95}}


class Env:
    def __init__(self, stub: StubModels, tmp: Path) -> None:
        self.stub = stub
        self.calls: list[dict[str, Any]] = []
        self.key = tmp / "key"
        self.key.write_text("sk-test-secret\n")
        self.tmp = tmp

    def provider(self, kind: str, **over: Any) -> ChatProvider | DecisionsProvider:
        raw = {"kind": kind, "base_url": self.stub.url, "api_key_file": str(self.key), **over}
        return build_provider("p", parse_provider("p", raw))

    def decider(self, kind: str = "chat", task: str = "quality.diagnose", *, prov: dict[str, Any] | None = None,
                **tcfg: Any) -> Decider:
        t = TaskConfig(**{"mode": "model", "provider": "p", "model": "m", **tcfg})
        return Decider({task: t}, {"p": self.provider(kind, **(prov or {}))}, self.calls.append)


@pytest.fixture
def env(tmp_path: Path) -> Any:
    with StubModels() as stub:
        yield Env(stub, tmp_path)


def script(env: Env, chat: Any = None, dec: Any = None) -> None:
    env.stub.script = {"/chat/completions": env.stub._listify(chat), "/decisions": env.stub._listify(dec)}


# ------------------------------------------------------------------ providers
def test_chat_request_shape_and_reply(env: Env) -> None:
    script(env, chat=chat_body(GOOD_CHAT))
    p = env.provider("chat", json_schema=True, headers={"X-Title": "osprey"})
    r = p.complete("m1", "sys", "hello", [JPEG], {"type": "object"})  # type: ignore[union-attr]
    assert r.data["problem"] == "glare" and (r.tokens_in, r.tokens_out, r.cost) == (120, 30, 0.0002)
    req = env.stub.requests[0]
    assert req["path"] == "/chat/completions"
    assert req["headers"]["authorization"] == "Bearer sk-test-secret" and req["headers"]["x-title"] == "osprey"
    body = req["json"]
    assert body["model"] == "m1" and body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    parts = body["messages"][1]["content"]
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64," + JPEG_B64


def test_chat_json_object_and_embedded_json(env: Env) -> None:
    script(env, chat=chat_body('Sure! {"a": {"b": 1}} done'))
    r = env.provider("chat").complete("m", "s", "u", [], None)  # type: ignore[union-attr]
    assert r.data == {"a": {"b": 1}}
    assert env.stub.requests[0]["json"]["response_format"] == {"type": "json_object"}


def test_key_from_env_and_missing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    script(env, chat=chat_body({"x": 1}))
    monkeypatch.setenv("ASSIST_KEY", "envkey")
    p = build_provider("p", parse_provider("p", {"kind": "chat", "base_url": env.stub.url, "api_key_env": "ASSIST_KEY"}))
    p.complete("m", "s", "u", [], None)  # type: ignore[union-attr]
    assert env.stub.requests[0]["headers"]["authorization"] == "Bearer envkey"
    bad = build_provider("p", parse_provider("p", {"kind": "chat", "base_url": env.stub.url, "api_key_file": "/nope/key"}))
    with pytest.raises(ProviderError):
        bad.complete("m", "s", "u", [], None)  # type: ignore[union-attr]


def test_decisions_request_shape(env: Env) -> None:
    script(env, dec=decisions_body(GOOD_DEC))
    p = env.provider("decisions")
    r = p.decide("dm", ["text", JPEG], {"q": {"type": "noul", "instructions": "i"}})  # type: ignore[union-attr]
    assert r.data["problem"]["choice"] == "glare" and r.cost == 0.0001 and r.model == "stub-decisions"
    req = env.stub.requests[0]
    assert req["path"] == "/decisions" and req["json"]["model"] == "dm"
    assert req["json"]["state"][0] == "text"
    assert req["json"]["state"][1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + JPEG_B64}}
    assert req["headers"]["authorization"] == "Bearer sk-test-secret"


@pytest.mark.parametrize("bad", [HTTP_429, HTTP_500, MALFORMED, INVALID_JSON])
def test_provider_errors(env: Env, bad: str) -> None:
    script(env, chat=bad)
    with pytest.raises(ProviderError) as e:
        env.provider("chat").complete("m", "s", "u", [], None)  # type: ignore[union-attr]
    assert "sk-test" not in str(e.value)


def test_timeout(env: Env) -> None:
    env.stub.slow_sec = 1.5
    script(env, dec=SLOW)
    p = env.provider("decisions")
    p.cfg = dataclasses.replace(p.cfg, timeout_sec=0.3)
    with pytest.raises(ProviderError):
        p.decide("m", ["t"], {})  # type: ignore[union-attr]


def test_budget_window() -> None:
    now = [0.0]
    b = Budget(2, 3, lambda: now[0])
    b.record(); b.record()
    assert not b.allow() and b.left == 0
    now[0] = 3601
    assert b.allow() and b.calls_today == 2 and b.left == 1
    b.record()
    now[0] = 3700
    assert not b.allow()  # day cap
    now[0] = 100000
    assert b.allow() and b.calls_today == 0


# --------------------------------------------------------- strict validation
@pytest.mark.parametrize("raw", [
    {"kind": "chat"}, {"kind": "x", "base_url": "http://a"}, {"kind": "chat", "base_url": "ftp://a"},
    {"kind": "chat", "base_url": "http://a", "bogus": 1}, {"kind": "chat", "base_url": "http://a", "timeout_sec": 0},
    {"kind": "chat", "base_url": "http://a", "timeout_sec": 301},
    {"kind": "chat", "base_url": "http://a", "allow_frames": "yes"},
    {"kind": "decisions", "base_url": "http://a", "json_schema": True},
    {"kind": "chat", "base_url": "http://a", "max_calls_per_hour": 0},
    {"kind": "chat", "base_url": "http://a", "headers": {"Authorization": "x"}},
    {"kind": "chat", "base_url": "http://a", "path": "chat"},
])
def test_parse_provider_rejects(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_provider("p", raw)


def test_parse_provider_defaults() -> None:
    c = parse_provider("p", {"kind": "decisions", "base_url": "http://a/"})
    assert (c.path, c.base_url, c.allow_frames, c.timeout_sec, c.max_calls_per_hour, c.max_calls_per_day) == (
        "/decisions", "http://a", False, 30, 120, 2000)
    assert parse_provider("p", {"kind": "chat", "base_url": "http://a"}).path == "/chat/completions"


@pytest.mark.parametrize("raw", [
    {"mode": "model"}, {"mode": "both", "provider": "p"}, {"mode": "nope"}, {"on_disagree": "x"},
    {"min_confidence": 1.5}, {"send_frames": 1}, {"zzz": 1},
])
def test_parse_task_rejects(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_task("quality.diagnose", raw)


def test_parse_task_defaults() -> None:
    assert parse_task("t", {}) == TaskConfig()
    assert parse_task("t", {"mode": "both", "provider": "p", "model": "m"}).on_disagree == "log"


# ------------------------------------------------------------------- diagnose
def user_text(env: Env) -> str:
    c = env.stub.requests[-1]["json"]["messages"][1]["content"]
    return str(c[0]["text"])


def test_rules_mode_makes_no_call(env: Env) -> None:
    d = Decider({"quality.diagnose": TaskConfig()}, {"p": env.provider("chat")}, env.calls.append)
    assert d.diagnose(CTX, RULES, JPEG) is RULES
    assert d.diagnose(CTX, None, None).problem == "ok"
    assert env.stub.requests == [] and env.calls == []


def test_chat_model_mode_happy_and_frames_policy(env: Env) -> None:
    script(env, chat=chat_body(GOOD_CHAT))
    # both opt-ins needed
    for prov_ok, task_ok, expect_img in [(True, True, True), (True, False, False), (False, True, False)]:
        env.stub.requests.clear()
        d = env.decider("chat", prov={"allow_frames": prov_ok}, send_frames=task_ok, explain=True)
        out = d.diagnose(CTX, RULES, JPEG)
        parts = env.stub.requests[0]["json"]["messages"][1]["content"]
        assert (len(parts) == 2) is expect_img
        assert out.problem == "glare" and out.source == "model:stub-chat"
        assert out.actions == (Action("exposure", -2.0),) and out.evidence == "hot spot top right"
    sysmsg = env.stub.requests[0]["json"]["messages"][0]["content"]
    assert "focusPosition" in sysmsg and "wdr" in sysmsg and "smallest" in sysmsg and "ask_human" in sysmsg
    txt = user_text(env)
    assert "glare=0.100" in txt and "exposure: range -6..6" in txt and "012345678" in txt and "Front Door" in txt
    ok = env.calls[-1]
    assert ok["ok"] is True and ok["task"] == "quality.diagnose" and ok["tokens_in"] == 120 and ok["error"] is None
    assert "text" not in ok and "prompt" not in ok


def test_redact_and_explain_default(env: Env) -> None:
    script(env, chat=chat_body(GOOD_CHAT))
    d = env.decider("chat")
    d.redact = True
    out = d.diagnose(CTX, None, None)
    assert "Front Door" not in user_text(env) and out.evidence == ""


def test_json_schema_levers_enum(env: Env) -> None:
    script(env, chat=chat_body(GOOD_CHAT))
    env.decider("chat", prov={"json_schema": True}).diagnose(CTX, None, None)
    sch = env.stub.requests[0]["json"]["response_format"]["json_schema"]["schema"]
    assert sch["properties"]["actions"]["items"]["properties"]["lever"]["enum"] == ["exposure", "gain", "sharpness"]


def test_deny_and_unknown_levers_dropped_and_clamped(env: Env) -> None:
    acts = [{"lever": "wdr", "change": 1}, {"lever": "irLedLevel", "change": 1}, {"lever": "mystery", "change": 1},
            {"lever": "gain", "change": 7.4}]
    script(env, chat=chat_body({**GOOD_CHAT, "actions": acts}))
    out = env.decider("chat").diagnose(CTX, RULES, None)
    assert out.actions == (Action("gain", 8.0),)  # step 2, only gain survives
    script(env, chat=chat_body({**GOOD_CHAT, "actions": [{"lever": "exposure", "change": -99}]}))
    out = env.decider("chat").diagnose(CTX, RULES, None)
    assert out.actions == (Action("exposure", -12.0),)  # bounded by range span


@pytest.mark.parametrize("bad", [
    {**GOOD_CHAT, "problem": "fog"}, {**GOOD_CHAT, "confidence": 2}, {**GOOD_CHAT, "actions": "x"},
    {**GOOD_CHAT, "actions": [{"lever": "exposure", "change": "big"}]}, INVALID_JSON, MALFORMED, HTTP_429, HTTP_500,
])
def test_invalid_answers_fall_back_to_rules(env: Env, bad: Any) -> None:
    script(env, chat=chat_body(bad) if isinstance(bad, dict) else bad)
    for mode in ("model", "both"):
        env.calls.clear()
        out = env.decider("chat", mode=mode).diagnose(CTX, RULES, None)
        assert out == RULES
        assert env.calls[0]["ok"] is False and env.calls[0]["error"]


def test_slow_provider_falls_back(env: Env) -> None:
    env.stub.slow_sec = 1.5
    script(env, chat=SLOW)
    d = env.decider("chat")
    p = d.providers["p"]
    p.cfg = dataclasses.replace(p.cfg, timeout_sec=0.3)
    assert d.diagnose(CTX, RULES, None) == RULES and env.calls[0]["ok"] is False


def test_low_confidence_asks_human(env: Env) -> None:
    script(env, chat=chat_body({**GOOD_CHAT, "confidence": 0.4}))
    out = env.decider("chat").diagnose(CTX, RULES, None)
    assert out.actions == () and out.ask_human and out.problem == "glare"


def test_metric_sanity_marks_disagree(env: Env) -> None:
    quiet = dataclasses.replace(CTX, metrics=dataclasses.replace(METRICS, glare=0.01))
    script(env, chat=chat_body(GOOD_CHAT))
    out = env.decider("chat").diagnose(quiet, RULES, None)
    assert out.disagree and out.actions == ()
    # blur needs z['sharp'] < -1
    script(env, chat=chat_body({**GOOD_CHAT, "problem": "blur", "actions": []}))
    assert env.decider("chat").diagnose(CTX, None, None).disagree
    blurry = dataclasses.replace(CTX, z={**CTX.z, "sharp": -2.0})
    assert not env.decider("chat").diagnose(blurry, None, None).disagree
    # both mode: an implausible model answer keeps the rules
    script(env, chat=chat_body(GOOD_CHAT))
    out = env.decider("chat", mode="both", on_disagree="model").diagnose(quiet, RULES, None)
    assert out.actions == RULES.actions and out.disagree


def test_both_agreement(env: Env) -> None:
    script(env, chat=chat_body({**GOOD_CHAT, "confidence": 0.95}))
    out = env.decider("chat", mode="both").diagnose(CTX, RULES, None)
    assert (out.problem, out.source, out.confidence, out.disagree) == ("glare", "both", 0.95, False)
    assert out.actions == (Action("exposure", -2.0),)
    # model valid but proposes only denied levers -> rules' actions
    script(env, chat=chat_body({**GOOD_CHAT, "actions": [{"lever": "wdr", "change": 1}]}))
    assert env.decider("chat", mode="both").diagnose(CTX, RULES, None).actions == RULES.actions


@pytest.mark.parametrize("policy", ["rules", "model", "log"])
def test_both_disagreement_policies(env: Env, policy: str) -> None:
    blur = {**GOOD_CHAT, "problem": "blur", "actions": [{"lever": "sharpness", "change": 1}]}
    script(env, chat=chat_body(blur))
    ctx = dataclasses.replace(CTX, z={**CTX.z, "sharp": -3.0})
    out = env.decider("chat", mode="both", on_disagree=policy).diagnose(ctx, RULES, None)
    if policy == "rules":
        assert out.problem == "glare" and out.actions == RULES.actions and out.disagree
    elif policy == "model":
        assert out.problem == "blur" and out.actions == (Action("sharpness", 1.0),) and out.disagree
    else:
        assert out.disagree and out.actions == () and out.source == "both"


def test_decisions_diagnose_happy(env: Env) -> None:
    script(env, dec=decisions_body(GOOD_DEC))
    out = env.decider("decisions", prov={"allow_frames": True}, send_frames=True).diagnose(CTX, RULES, JPEG)
    assert out.problem == "glare" and out.actions == (Action("exposure", -2.0),)
    assert out.confidence == pytest.approx(0.9 * 0.95)
    body = env.stub.requests[0]["json"]
    assert body["state"][1]["type"] == "image_url" and len(body["state"]) == 2
    q = body["questions"]
    assert set(q) == {"problem", "lever", "fix_needed"} and q["fix_needed"]["type"] == "noul"
    assert set(q["lever"]["criteria"]) == {"exposure", "gain", "sharpness", "none"}
    assert "ok" in q["problem"]["criteria"] and "glare" in q["problem"]["criteria"]
    env.stub.requests.clear()
    env.decider("decisions", prov={"allow_frames": True}, send_frames=False).diagnose(CTX, RULES, JPEG)
    assert len(env.stub.requests[0]["json"]["state"]) == 1


def test_decisions_never_invents_levers(env: Env) -> None:
    for lever in ("wdr", "sharpness", "none"):  # denied / belongs to blur / none
        script(env, dec=decisions_body({**GOOD_DEC, "lever": choice(lever, 0.9)}))
        out = env.decider("decisions").diagnose(CTX, RULES, None)
        assert out.problem == "glare" and out.actions == () and out.ask_human


def test_decisions_low_confidence_and_refusals(env: Env) -> None:
    script(env, dec=decisions_body({**GOOD_DEC, "fix_needed": {"type": "noul", "noul": 0.3}}))
    out = env.decider("decisions").diagnose(CTX, RULES, None)
    assert out.actions == () and out.ask_human
    for ans in ({**GOOD_DEC, "problem": {"type": "refusal"}}, {}, {**GOOD_DEC, "problem": choice("fog", 0.9)}):
        script(env, dec=decisions_body(ans))
        assert env.decider("decisions").diagnose(CTX, RULES, None) == RULES
    script(env, dec=decisions_body({**GOOD_DEC, "lever": {"type": "refusal"}}))
    assert env.decider("decisions").diagnose(CTX, RULES, None).actions == ()


def test_decisions_ok_problem(env: Env) -> None:
    script(env, dec=decisions_body({"problem": choice("ok", 0.95), "lever": choice("none", 0.9),
                                    "fix_needed": {"type": "noul", "noul": 0.01}}))
    out = env.decider("decisions").diagnose(CTX, RULES, None)
    assert out.problem == "ok" and out.actions == () and not out.ask_human and out.confidence == 0.95


def test_budget_exhaustion_runs_rules_only(env: Env) -> None:
    script(env, chat=chat_body(GOOD_CHAT))
    d = env.decider("chat", prov={"max_calls_per_hour": 1})
    assert d.diagnose(CTX, None, None).source.startswith("model")
    assert d.diagnose(CTX, RULES, None) == RULES
    assert len(env.stub.requests) == 1
    assert env.calls[-1]["ok"] is False and env.calls[-1]["error"] == "budget"


def test_missing_provider_or_task_is_rules(env: Env) -> None:
    d = Decider({"quality.diagnose": TaskConfig(mode="model", provider="gone", model="m")}, {})
    assert d.diagnose(CTX, RULES, None) is RULES
    assert Decider({}, {}).judge(JCTX, None, None, None).better == "same"


# ---------------------------------------------------------------------- judge
RV = Verdict("after", 0.6, "rules", "metrics improved")


def test_judge_chat_with_frames(env: Env) -> None:
    script(env, chat=chat_body({"better": "after", "confidence": 0.9, "reason": "glare gone"}))
    d = env.decider("chat", "quality.judge", prov={"allow_frames": True}, send_frames=True, explain=True)
    v = d.judge(JCTX, JPEG, JPEG + b"2", RV)
    assert (v.better, v.confidence, v.reason) == ("after", 0.9, "glare gone") and v.source == "model:stub-chat"
    parts = env.stub.requests[0]["json"]["messages"][1]["content"]
    assert len(parts) == 3 and "BEFORE" in parts[0]["text"] and "glare=0.010" in parts[0]["text"]
    assert env.calls[0]["task"] == "quality.judge"


def test_judge_text_only_without_permission(env: Env) -> None:
    script(env, chat=chat_body({"better": "same", "confidence": 0.9, "reason": ""}))
    env.decider("chat", "quality.judge", send_frames=True).judge(JCTX, JPEG, JPEG, None)
    parts = env.stub.requests[0]["json"]["messages"][1]["content"]
    assert len(parts) == 1 and "No images" in parts[0]["text"] and "after: " in parts[0]["text"]


def test_judge_decisions_and_low_confidence(env: Env) -> None:
    script(env, dec=decisions_body({"better": choice("after", 0.85, ("before", "same"))}))
    d = env.decider("decisions", "quality.judge", prov={"allow_frames": True}, send_frames=True)
    v = d.judge(JCTX, JPEG, JPEG, RV)
    assert (v.better, v.confidence) == ("after", 0.85)
    state = env.stub.requests[0]["json"]["state"]
    assert len(state) == 3 and state[1]["type"] == "image_url"
    assert env.stub.requests[0]["json"]["questions"]["better"]["type"] == "choice"
    script(env, dec=decisions_body({"better": choice("after", 0.5)}))
    assert d.judge(JCTX, JPEG, JPEG, RV).better == "same"


def test_judge_both_and_fallback(env: Env) -> None:
    script(env, chat=chat_body({"better": "before", "confidence": 0.9, "reason": "r"}))
    for policy, better in [("rules", "after"), ("model", "before"), ("log", "same")]:
        v = env.decider("chat", "quality.judge", mode="both", on_disagree=policy).judge(JCTX, None, None, RV)
        assert v.better == better and v.source == "both"
    script(env, chat=chat_body({"better": "after", "confidence": 0.9, "reason": "r"}))
    v = env.decider("chat", "quality.judge", mode="both").judge(JCTX, None, None, RV)
    assert (v.better, v.confidence, v.source) == ("after", 0.9, "both")
    for bad in (HTTP_500, chat_body({"better": "maybe", "confidence": 0.9, "reason": ""}), MALFORMED):
        script(env, chat=bad)
        assert env.decider("chat", "quality.judge").judge(JCTX, None, None, RV) == RV
    assert env.calls[-1]["ok"] is False
