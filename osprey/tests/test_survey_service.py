"""Survey's HTTP API, the console path, Home Assistant, the command line and the config."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import assist.config as acfg
from assist import cli
from assist.notify import Callbacks, Notifier, parse_notify
from assist.server import AssistServer
from assist_world import TOKEN
from stub_models import StubModels
from survey_world import decisions_reply
from test_assist_notify import FakeAdapter
from test_survey import SRig
from test_webapi import Client


class Stack(SRig):
    def __init__(self, tmp: Path, models: StubModels, **kw: Any) -> None:
        super().__init__(tmp, models=models, **kw)
        self.server = AssistServer(("127.0.0.1", 0), self.engine, str(self.world.token_file))
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method: str, path: str, body: object = None, token: str | None = TOKEN) -> tuple[int, Any]:
        req = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as reply:
                raw = reply.read()
                return reply.status, (json.loads(raw) if reply.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def finish(self) -> dict[str, Any]:
        self.svc.join(120)
        status, body = self.call("GET", "/survey/status")
        assert status == 200
        return dict(body)

    def close(self) -> None:
        self.server.shutdown()
        super().close()


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    with StubModels(decisions_reply=decisions_reply) as models:
        s = Stack(tmp_path, models)
        try:
            yield s
        finally:
            s.close()


def test_start_follow_review_apply_revert_over_http(stack: Stack) -> None:
    assert stack.call("GET", "/survey/status", token=None)[0] == 401
    assert stack.call("GET", "/survey/status")[1]["first_run"] is True
    status, body = stack.call("POST", "/survey/start", {})
    assert status == 200 and body["ok"]
    assert stack.call("POST", "/survey/start", {})[0] == 409          # one at a time
    done = stack.finish()
    assert done["run"]["status"] == "ready" and done["maps"]["day"]["regions"] > 5 and done["pending"] >= 3
    status, smap = stack.call("GET", "/survey/map?kind=day")
    assert status == 200 and smap["proposals"] and smap["extent"][0] < -200
    assert stack.call("GET", "/survey/panorama.jpg?kind=day")[1].startswith(b"\xff\xd8")
    assert stack.call("GET", "/survey/overlay.png?kind=day")[1].startswith(b"\x89PNG")
    assert stack.call("GET", "/survey/map?kind=night")[0] == 404
    assert stack.call("GET", "/survey/map?kind=dusk")[0] == 409
    door = next(p for p in smap["proposals"] if p.get("name") == "Door")
    assert stack.call("GET", f"/survey/proposals/{door['id']}/preview.jpg")[1].startswith(b"\xff\xd8")
    assert stack.call("GET", "/survey/preview.jpg?kind=day&pan=0.1&tilt=0&zoom=0.3")[1].startswith(b"\xff\xd8")
    assert stack.call("GET", "/survey/frigate.json?kind=day")[1]["presets"]
    assert stack.call("POST", f"/survey/proposals/{door['id']}/goto", {})[0] == 200
    assert stack.world.view()[2] > 0.3 and stack.world.preset_names() == []
    assert stack.call("POST", f"/survey/proposals/{door['id']}/accept", {})[0] == 200
    assert stack.world.preset_names() == ["Door"]
    assert stack.call("POST", "/survey/apply", {"ids": ["x"]})[0] == 409
    assert stack.call("POST", "/survey/apply", {})[0] == 200
    assert {"Gate", "Driveway"} <= set(stack.world.preset_names())
    assert stack.call("POST", "/survey/revert", {})[0] == 200
    assert stack.world.preset_names() == ["Door"]                      # the one accepted earlier was a separate apply
    assert stack.call("POST", "/survey/proposals/999/accept", {})[0] == 409
    assert stack.call("GET", "/survey/nonsense?kind=day")[0] == 404


def test_cancel_over_http_and_the_mode_setting(stack: Stack) -> None:
    stack.world.on_move = lambda n: stack.svc.cancel() if n == 3 else None
    assert stack.call("POST", "/survey/start", {})[0] == 200
    assert stack.finish()["run"]["status"] == "cancelled"
    assert stack.call("POST", "/settings", {"capabilities": {"survey": {"mode": "observe"}}})[1]["capabilities"]["survey"]["mode"] == "observe"
    assert stack.call("POST", "/survey/apply", {})[0] == 409
    assert stack.call("POST", "/settings", {"capabilities": {"survey": {"mode": "off"}}})[0] == 200
    assert stack.call("POST", "/survey/start", {})[0] == 409
    assert stack.call("POST", "/survey/start", {"camera": "nope"})[0] == 409


def test_the_console_reaches_survey_through_osprey(stack: Stack) -> None:
    import assist_scope
    stack.world.server.web.assist = assist_scope.AssistLink(TOKEN, frozenset({"quality", "presets", "patrol"}), stack.url)
    console = Client(stack.world.server)
    console.session()
    assert console.json("POST", "/api/v1/assist/survey/start", {"camera": "default"})[0] == 200
    stack.finish()
    status, body = console.json("GET", "/api/v1/assist/survey/status?camera=default")
    assert status == 200 and body["run"]["status"] == "ready"
    status, _, raw = console.request("GET", "/api/v1/assist/survey/overlay.png?camera=default&kind=day")
    assert status == 200 and raw.startswith(b"\x89PNG")
    status, _, raw = console.request("GET", "/api/v1/assist/survey/panorama.jpg?camera=default&kind=day")
    assert status == 200 and raw.startswith(b"\xff\xd8")
    status, _, raw = console.request("GET", "/api/v1/assist/survey/preview.jpg?kind=day&pan=0&tilt=0&zoom=0")
    assert status == 200 and raw.startswith(b"\xff\xd8")
    assert console.json("GET", "/api/v1/assist/survey/../data")[0] in (404, 405)
    assert console.json("DELETE", "/api/v1/assist/survey/start")[0] == 404


def test_home_assistant_gets_a_start_button_and_a_status(stack: Stack) -> None:
    adapter = FakeAdapter()

    def start() -> None:
        stack.svc.start("default", "manual")

    notifier = Notifier(parse_notify({"base_topic": "osprey", "device": "default"}), adapter, Callbacks(
        lambda *a: None, stack.engine.set_enabled, stack.engine.set_mode, lambda: None, lambda: None,
        start_survey=start, cancel_survey=stack.svc.cancel), stack.store.frame)
    stack.engine.notifier = notifier
    notifier.start()
    topics = adapter.topics()
    assert any("survey_start" in t and "button" in t for t in topics) and any("survey_status" in t for t in topics)
    assert any("survey_progress" in t for t in topics) and "osprey/default/assist/command/mode/survey" in adapter.subs
    topic = "osprey/default/assist/command/survey_start"
    adapter.subs[topic](topic, b"PRESS", False)
    stack.finish()
    assert stack.svc.db.runs("default")[0]["status"] == "ready"
    assert adapter.last("osprey/default/assist/state/survey") == b"ready"
    assert int(adapter.last("osprey/default/assist/state/survey_pending")) >= 3
    assert adapter.last("osprey/default/assist/state/survey_progress") == b"100"


def test_the_command_line_plans_starts_waits_and_reverts(stack: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    host, port = stack.server.server_address[:2]
    config = tmp_path / "osprey-assist.json"
    config.write_text(json.dumps({"enabled": True, "storage": {"dir": str(tmp_path / "s")},
                                  "osprey": {"url": stack.world.client.url, "token_file": str(stack.world.token_file)},
                                  "listen": {"bind": str(host), "port": port},
                                  "survey": {"settle_sec": 1, "zoom_settle_sec": 1}}), encoding="utf-8")
    assert cli.main(["--config", str(config), "survey", "--plan"]) == 0
    out = capsys.readouterr().out
    assert "6 columns by 4 rows, 24 pictures" in out and "minutes" in out
    assert cli.main(["--config", str(config), "survey", "--wait"]) == 0
    out = capsys.readouterr().out
    assert "Survey started." in out and "ready" in out and "proposal(s) wait" in out
    assert cli.main(["--config", str(config), "survey", "--apply"]) == 0
    assert "Door" in stack.world.preset_names()
    assert cli.main(["--config", str(config), "survey", "--revert"]) == 0
    assert stack.world.preset_names() == []
    assert cli.main(["--config", str(config), "survey", "--status"]) == 0
    assert cli.main(["--config", str(config), "survey", "--cancel"]) == 0
    capsys.readouterr()


def test_the_command_line_says_when_assist_is_not_running(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "osprey-assist.json"
    (tmp_path / "t").write_text("x" * 40)
    config.write_text(json.dumps({"osprey": {"token_file": str(tmp_path / "t")}, "listen": {"port": 1}}), encoding="utf-8")
    assert cli.main(["--config", str(config), "survey", "--status"]) == 2
    assert "not answering" in capsys.readouterr().err


# ----------------------------------------------------------------------- config

def test_survey_config_defaults_and_validation() -> None:
    cfg = acfg.parse({})
    assert cfg.modes["survey"] == "suggest" and cfg.survey.schedule == acfg.Schedule("11:00", "02:00", 7)
    assert cfg.survey.patrol_policy == "merge" and cfg.survey.tiles == (4, 3)
    assert acfg.parse({"survey": {"schedule": None}}).survey.schedule is None
    for bad in ({"overlap": 0.1}, {"tiles": [0, 3]}, {"patrol_policy": "wipe"}, {"surprise": 1}, {"dwell": {"base_sec": 2, "min_sec": 5}},
                {"schedule": {"day": "25:00"}}, {"frigate": {"url": "ftp://x"}}, {"fov": {"pan_degrees": 0}}, {"projection": "fisheye"}):
        with pytest.raises(ValueError):
            acfg.parse({"survey": bad})


def test_a_survey_model_task_must_be_allowed_to_see_pictures() -> None:
    providers = {"gw": {"kind": "decisions", "base_url": "http://127.0.0.1:1", "allow_frames": True}}
    with pytest.raises(ValueError, match="send_frames"):
        acfg.parse({"providers": providers, "tasks": {"survey.label": {"mode": "model", "provider": "gw", "model": "m"}}})
    ok = acfg.parse({"providers": providers, "tasks": {"survey.label": {"mode": "model", "provider": "gw", "model": "m", "send_frames": True}}})
    assert ok.tasks["survey.label"].mode == "model"


def test_without_a_model_the_rules_still_label_sky_and_foliage(tmp_path: Path) -> None:
    rig = SRig(tmp_path)
    try:
        run = rig.run()
        assert run["status"] == "ready"
        smap = rig.svc.db.latest_map("default", "day")
        assert smap is not None and smap["used_model"] is False
        assert {"sky", "foliage"} <= {r["label"] for r in smap["regions"]}
    finally:
        rig.close()


def test_a_model_that_misbehaves_costs_labels_not_the_survey(tmp_path: Path) -> None:
    from stub_models import HTTP_500, MALFORMED
    with StubModels(decisions_reply=[MALFORMED, HTTP_500, decisions_reply]) as models:
        rig = SRig(tmp_path, models=models)
        try:
            assert rig.run()["status"] == "ready"
        finally:
            rig.close()
    with StubModels(decisions_reply={"answers": {"kind": {"type": "choice", "choice": "unicorn"}}}) as models:
        (tmp_path / "b").mkdir()
        rig = SRig(tmp_path / "b", models=models)
        try:
            run = rig.run()
            assert run["status"] == "ready"
            smap = rig.svc.db.latest_map("default", "day")
            assert smap is not None and "unicorn" not in {r["label"] for r in smap["regions"]}
        finally:
            rig.close()


def test_the_model_budget_leaves_the_rest_to_the_rules(tmp_path: Path) -> None:
    with StubModels(decisions_reply=decisions_reply) as models:
        rig = SRig(tmp_path, models=models)
        try:
            rig.engine.decider.providers["stub"].budget.max_per_hour = 10
            run = rig.run()
            assert run["status"] == "ready"
            assert len(models.requests) <= 12
        finally:
            rig.close()
    assert time.time() > 0


def test_extra_body_is_sent_with_every_model_request(tmp_path: Path) -> None:
    with StubModels(decisions_reply=decisions_reply) as models:
        cfg = acfg.parse({"providers": {"gw": {"kind": "decisions", "base_url": models.url,
                                               "extra_body": {"provider": {"data_collection": "deny"}}}}})
        from assist.providers import DecisionsProvider
        DecisionsProvider("gw", cfg.providers["gw"]).decide("m", ["x"], {"q": {"type": "noul", "instructions": "?"}})
        assert models.requests[0]["json"]["provider"] == {"data_collection": "deny"} and models.requests[0]["json"]["model"] == "m"
    with pytest.raises(ValueError, match="extra_body"):
        acfg.parse({"providers": {"gw": {"kind": "decisions", "base_url": "http://x", "extra_body": 3}}})
