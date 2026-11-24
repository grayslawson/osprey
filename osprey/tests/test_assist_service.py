"""Assist's models, HTTP API, console path and notifications, around the simulated camera."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import assist.config as acfg
from assist import cli
from assist.decide import Decider
from assist.engine import Engine
from assist.notify import Callbacks, Notifier, parse_notify
from assist.providers import build_provider
from assist.server import AssistServer
from assist.store import Store
from assist_world import TOKEN, Clock, World
from stub_models import StubModels, decisions_body
from test_assist_loop import goto
from test_assist_notify import FakeAdapter
from test_webapi import Client


class Stack:
    def __init__(self, tmp: Path, stub: StubModels | None = None, *, task: dict[str, Any] | None = None,
                 mode: str = "suggest", provider: dict[str, Any] | None = None) -> None:
        self.clock = Clock()
        self.world = World(tmp, self.clock)
        raw: dict[str, Any] = {"enabled": True, "storage": {"dir": str(tmp / "state")}, "osprey": {"token_file": str(self.world.token_file)},
                               "listen": {"port": 1}, "capabilities": {"quality": {"mode": mode, "settle_sec": 1, "isp_settle_sec": 1}}}
        if stub is not None:
            raw["providers"] = {"gw": {"kind": "decisions", "base_url": stub.url, "path": "/decisions", "allow_frames": True,
                                       **(provider or {})}}
            raw["tasks"] = {"quality.diagnose": {"mode": "model", "provider": "gw", "model": "stub-d", "send_frames": True, **(task or {})}}
        self.cfg = acfg.parse(raw)
        self.store = Store(self.cfg.state_dir, clock=self.clock)
        providers = {n: build_provider(n, p) for n, p in self.cfg.providers.items()}
        self.engine = Engine(self.cfg, self.store, {"default": self.world.client}, Decider(self.cfg.tasks, providers, self.store.log_call),
                             clock=self.clock, sleep=self.clock.sleep)
        self.server = AssistServer(("127.0.0.1", 0), self.engine, str(self.world.token_file))
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method: str, path: str, body: object = None, token: str | None = TOKEN) -> tuple[int, Any]:
        req = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as reply:
                raw = reply.read()
                return reply.status, (json.loads(raw) if reply.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def warm_and_detect(self) -> int:
        goto(self, "BackPorch")
        self.world.glare, glare = None, self.world.glare
        for _ in range(10):
            self.engine.check("default", "manual")
            self.clock.now += 700
        self.world.glare = glare
        return next(i for i in [self.engine.check("default", "arrival") for _ in range(2)] if i)

    def close(self) -> None:
        self.server.shutdown()
        self.world.close()
        self.store.close()


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    s = Stack(tmp_path)
    try:
        yield s
    finally:
        s.close()


GLARE_ANSWER = {
    "problem": {"type": "choice", "choice": "glare", "probabilities": {"glare": 0.92, "ok": 0.08}},
    "lever": {"type": "choice", "choice": "night_view", "probabilities": {"night_view": 0.9, "none": 0.1}},
    "fix_needed": {"type": "noul", "noul": 0.95},
}


def test_a_decision_model_can_lead_the_diagnosis(tmp_path: Path) -> None:
    with StubModels(decisions_reply=decisions_body(GLARE_ANSWER)) as stub:
        s = Stack(tmp_path, stub)
        try:
            decision = s.warm_and_detect()
            row = s.store.decision(decision)
            assert row is not None and row["source"].startswith("model:") and row["status"] == "pending"
            request = next(r for r in stub.requests if r["path"] == "/decisions")
            assert any(isinstance(x, dict) and x.get("type") == "image_url" for x in request["json"]["state"])
            assert s.store.calls_today() >= 1
        finally:
            s.close()


def test_frames_stay_home_unless_both_sides_allow_them(tmp_path: Path) -> None:
    with StubModels(decisions_reply=decisions_body(GLARE_ANSWER)) as stub:
        s = Stack(tmp_path, stub, task={"send_frames": False})
        try:
            s.warm_and_detect()
            sent = [r for r in stub.requests if r["path"] == "/decisions"]
            assert sent and not any(isinstance(x, dict) for r in sent for x in r["json"]["state"])
        finally:
            s.close()


def test_a_dead_model_falls_back_to_the_rules(tmp_path: Path) -> None:
    with StubModels(decisions_reply="http_500") as stub:
        s = Stack(tmp_path, stub, task={"mode": "both"})
        try:
            decision = s.warm_and_detect()
            row = s.store.decision(decision)
            assert row is not None and row["source"] == "rules"
            failed = s.store._run("SELECT COUNT(*) AS c FROM model_calls WHERE ok=0").fetchone()["c"]
            assert failed >= 1
        finally:
            s.close()


def test_the_http_api_needs_the_token_and_drives_the_loop(stack: Stack) -> None:
    assert stack.call("GET", "/status", token=None)[0] == 401
    assert stack.call("GET", "/status", token="nope")[0] == 401
    decision = stack.warm_and_detect()
    status, body = stack.call("GET", "/status")
    assert status == 200 and body["pending"] == 1 and body["capabilities"]["quality"]["mode"] == "suggest"
    assert body["implemented"] == ["quality"] and "budgets" in body
    status, body = stack.call("GET", "/decisions?status=pending&limit=5")
    assert status == 200 and [d["id"] for d in body["decisions"]] == [decision]
    first = body["decisions"][0]
    assert first["change"].startswith("New night view") and len(first["glare_map"]) == 96
    status, raw = stack.call("GET", f"/frames/{first['after_frame']}.jpg")
    assert status == 200 and raw.startswith(b"\xff\xd8")
    assert stack.call("GET", "/frames/..%2f..%2fetc%2fpasswd.jpg")[0] == 404
    assert stack.call("POST", f"/decisions/{decision}/reject", {"reason": "because"})[0] == 409
    status, body = stack.call("POST", f"/decisions/{decision}/approve", {})
    assert status == 200 and body["status"] == "applied"
    assert "BackPorchNight" in stack.world.preset_names()
    assert stack.call("POST", f"/decisions/{decision}/approve", {})[0] == 409  # no double apply
    status, body = stack.call("POST", f"/decisions/{decision}/undo", {})
    assert status == 200 and body["status"] == "undone"


def test_settings_change_modes_and_the_kill_switch(stack: Stack) -> None:
    status, body = stack.call("POST", "/settings", {"capabilities": {"quality": {"mode": "observe"}}})
    assert status == 200 and body["capabilities"]["quality"]["mode"] == "observe"
    assert stack.call("POST", "/settings", {"capabilities": {"patrol": {"mode": "act"}}})[0] == 409
    assert stack.call("POST", "/settings", {"capabilities": {"quality": {"mode": "reckless"}}})[0] == 409
    assert stack.call("POST", "/settings", {"surprise": 1})[0] == 409
    status, body = stack.call("POST", "/settings", {"enabled": False})
    assert status == 200 and body["enabled"] is False
    assert stack.call("POST", "/check", {})[0] == 409  # off means off


def test_forget_everything(stack: Stack) -> None:
    stack.warm_and_detect()
    assert stack.call("DELETE", "/data")[0] == 200
    assert stack.store.decisions() == [] and list(stack.store.frames_dir.glob("*/*.jpg")) == []


def test_the_console_reaches_assist_through_osprey(stack: Stack) -> None:
    import assist_scope
    stack.world.server.web.assist = assist_scope.AssistLink(TOKEN, frozenset({"quality"}), stack.url)
    console = Client(stack.world.server)
    console.session()
    decision = stack.warm_and_detect()
    status, body = console.json("GET", "/api/v1/assist/decisions?status=pending")
    assert status == 200 and body["decisions"][0]["id"] == decision
    status, data = console.json("POST", f"/api/v1/assist/decisions/{decision}/reject", {"reason": "not_a_problem"})
    assert status == 200 and data["status"] == "rejected"
    status, _, raw = console.request("GET", f"/api/v1/assist/frames/{body['decisions'][0]['before_frame']}.jpg")
    assert status == 200 and raw.startswith(b"\xff\xd8")
    status, state = console.json("GET", "/api/v1/cameras/default/state")
    assert state["assist"] == {"enabled": True}


def test_home_assistant_can_approve_over_mqtt(stack: Stack) -> None:
    adapter = FakeAdapter()
    def decide(i: int, verdict: str, reason: str, principal: str) -> None:
        if verdict == "approve":
            stack.engine.approve(i, principal)
        else:
            stack.engine.reject(i, principal, reason)

    notifier = Notifier(parse_notify({"base_topic": "osprey", "device": "default"}), adapter, Callbacks(
        decide,
        stack.engine.set_enabled, stack.engine.set_mode, lambda: None,
        lambda: next(iter(stack.engine.view(r) for r in stack.store.decisions(statuses=("pending",), limit=1)), None)), stack.store.frame)
    stack.engine.notifier = notifier
    notifier.start()
    decision = stack.warm_and_detect()
    assert any("assist" in t and "suggestion" in t.lower() for t in adapter.topics())
    topic = "osprey/default/assist/command/decision"
    adapter.subs[topic](topic, json.dumps({"id": decision, "verdict": "approve"}).encode(), False)
    assert stack.store.decision(decision)["status"] == "applied"  # type: ignore[index]


def test_eval_scores_rules_against_labelled_frames(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import assist_scenes as sc
    from assist_world import write_cases
    clean = sc.scene(night=True, seed=3)
    write_cases(tmp_path / "cases", {"a": ("ok", clean), "b": ("glare", sc.add_glare(clean, 0.9, 0.4, 0.3)),
                                     "c": ("over", sc.expose(clean, 3.0))})
    config = tmp_path / "osprey-assist.json"
    config.write_text(json.dumps({"enabled": True, "storage": {"dir": str(tmp_path / "s")}}), encoding="utf-8")
    out = tmp_path / "result.json"
    assert cli.main(["--config", str(config), "eval", "--cases", str(tmp_path / "cases"), "--out", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["cases"] == 3 and summary["mode"] == "rules" and summary["rules_accuracy"] >= 0.66
    assert len(json.loads(out.read_text())["detail"]) == 3


def test_eval_can_try_a_model_against_the_same_frames(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import assist_scenes as sc
    from assist_world import write_cases
    write_cases(tmp_path / "cases", {"b": ("glare", sc.add_glare(sc.scene(night=True, seed=3), 0.9, 0.4, 0.3))})
    with StubModels(decisions_reply=decisions_body(GLARE_ANSWER)) as stub:
        config = tmp_path / "osprey-assist.json"
        config.write_text(json.dumps({"enabled": True, "storage": {"dir": str(tmp_path / "s")},
                                      "providers": {"gw": {"kind": "decisions", "base_url": stub.url, "allow_frames": True}}}),
                          encoding="utf-8")
        assert cli.main(["--config", str(config), "eval", "--cases", str(tmp_path / "cases"), "--mode", "model",
                         "--provider", "gw", "--model", "stub-d", "--send-frames"]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["accuracy"] == 1.0 and summary["model_calls"] == 1 and summary["usable_rate"] == 1.0
        assert summary["cost"] == pytest.approx(0.0001)


def test_purge_wants_a_yes(tmp_path: Path) -> None:
    config = tmp_path / "osprey-assist.json"
    config.write_text(json.dumps({"storage": {"dir": str(tmp_path / "s")}}), encoding="utf-8")
    assert cli.main(["--config", str(config), "purge"]) == 2
    assert cli.main(["--config", str(config), "purge", "--yes"]) == 0
