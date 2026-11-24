"""The core hooks Osprey Assist relies on: priority, token scope, patrol overrides, proxy."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import arbitration
import assist_scope
import config
import onvif
from arbitration import ASSIST, AUTO, MANUAL, Arbiter
from model import Position
from sentry import PatrolPoint, SentryPatrol
from sentry_manager import SentryManager
from sentry_runtime import MovementGate
from test_adoption_flow import _ptz_harness
from test_arbitration import MAC, Clock, _as
from test_webapi import Client, make

TOKEN = "t" * 40


# ------------------------------------------------------------- arbitration


def test_assist_ranks_like_the_patrol() -> None:
    clock = Clock()
    arbiter = Arbiter(30, clock=clock)
    assert arbiter.admit(MAC, ASSIST) == (True, False)
    assert arbiter.admit(MAC, AUTO) == (True, True)  # an autotracker takes the camera from Assist
    assert arbiter.admit(MAC, ASSIST) == (True, False)  # Assist never displaces it
    assert arbiter.admit(MAC, MANUAL) == (True, True)
    assert arbiter.admit(MAC, ASSIST) == (False, False)  # a person holds the camera
    assert arbiter.remaining(MAC) == 30  # and Assist never started that hold


def test_assist_moves_never_count_as_autotracking_and_are_refused_not_swallowed() -> None:
    ctl, session, wire = _ptz_harness()
    assert _as(ASSIST, lambda: ctl.move(MAC, Position(pan=1000, tilt=9000)))
    assert ctl.auto_age(MAC) is None
    assert not ctl.auto_active(MAC, 60)
    assert _as(AUTO, lambda: ctl.move(MAC, Position(pan=2000, tilt=9000)))
    assert ctl.auto_age(MAC) is not None
    _as(MANUAL, lambda: ctl.move(MAC, Position(pan=3000, tilt=9000)))
    sent = len(wire.sent())
    # Frigate is told it worked; Assist is told the truth.
    assert _as(AUTO, lambda: ctl.move(MAC, Position(pan=4000)))
    assert not _as(ASSIST, lambda: ctl.move(MAC, Position(pan=4000)))
    assert not _as(ASSIST, lambda: ctl.goto_preset(MAC, 1))
    assert len(wire.sent()) == sent


def test_a_new_scene_is_published_as_motion() -> None:
    ctl, session, wire = _ptz_harness()
    seen: list[str | None] = []
    ctl.on_motion = lambda camera: seen.append(camera.scene)
    from test_adoption_flow import _from_camera
    ctl._dispatch(session, _from_camera("EventIspSceneStatus", {"scene": "night"}, 90))
    assert seen[-1] == "NIGHT"


# ---------------------------------------------------------------- scope


def test_route_scopes() -> None:
    assert assist_scope.route_scope("GET", "/cameras/default/state") == "quality"
    assert assist_scope.route_scope("POST", "/cameras/default/presets/3/goto") == "quality"
    assert assist_scope.route_scope("POST", "/cameras/default/presets") == "presets"
    assert assist_scope.route_scope("POST", "/cameras/default/sentry") == "presets"
    for method, path in (("DELETE", "/cameras/default/sentry"), ("POST", "/cameras/default/ptz/recover"),
                         ("PUT", "/cameras/default/frigate"), ("GET", "/logs"), ("POST", "/session"),
                         ("DELETE", "/cameras/default/presets/3"), ("POST", "/assist/check")):
        assert assist_scope.route_scope(method, path) is None
    assert assist_scope.takes_lease("POST", "/cameras/default/ptz/move")
    assert not assist_scope.takes_lease("POST", "/cameras/default/isp")


def test_isp_allowlist_and_deny_list() -> None:
    assert assist_scope.check_isp({"aeTargetPercent": 45, "hdr": 1}) == {"aeTargetPercent": 45, "hdr": 1}
    bads: list[object] = [{"wdr": 1}, {"irLedLevel": 3}, {"focusPosition": 10}, {"irLedMode": 1}, {"nope": 1},
                          {"aeTargetPercent": 500}, {"hdr": True}, {"gain": float("nan")}, {"gain": "9"}, {}, []]
    for bad in bads:
        with pytest.raises(ValueError):
            assist_scope.check_isp(bad)


def test_scopes_and_token_file(tmp_path: Path) -> None:
    assert assist_scope.parse_scopes(None) == frozenset({"quality", "presets"})
    assert assist_scope.parse_scopes("quality") == frozenset({"quality"})
    with pytest.raises(ValueError):
        assist_scope.parse_scopes("admin")
    path = tmp_path / "state" / "assist-token"
    token = assist_scope.load_token(path)
    assert len(token) >= 32 and path.stat().st_mode & 0o777 == 0o600
    assert assist_scope.load_token(path) == token  # never rotated by a restart
    path.write_text("short")
    with pytest.raises(ValueError):
        assist_scope.load_token(path)


def test_assist_config_block() -> None:
    assert config.validate_assist(None)["enabled"] is False
    bads: list[object] = [{"enabled": 1}, {"url": "ftp://x"}, {"scopes": []}, {"scopes": ["root"]}, {"extra": 1}, []]
    for bad in bads:
        with pytest.raises(ValueError):
            config.validate_assist(bad)


# ----------------------------------------------------------------- patrol


def _patrol(moves: list[str], **kwargs: Any) -> SentryPatrol:
    def goto(name: str) -> bool:
        moves.append(name)
        return True

    patrol = SentryPatrol(goto_preset=goto, tracking_active=lambda: False,
                          camera_idle=lambda: True, interval_sec=10, **kwargs)
    patrol.configure([PatrolPoint("a"), PatrolPoint("b", 20)])
    patrol.enable(now=0)
    return patrol


def test_a_hold_stands_the_patrol_down_then_expires_on_its_own() -> None:
    moves: list[str] = []
    patrol = _patrol(moves)
    patrol.hold(30, "trial", now=5)
    assert not patrol.step(now=10) and patrol.snapshot().state == "assist"
    assert patrol.hold_remaining(now=10) == 25
    assert not patrol.step(now=34) and not moves
    assert patrol.step(now=36) and moves == ["a"]  # the hold lapsed with nobody releasing it
    patrol.hold(30, now=40)
    patrol.release()
    assert patrol.hold_remaining(now=41) == 0


def test_dwell_override_is_bounded_ignores_unknown_points_and_expires() -> None:
    moves: list[str] = []
    patrol = _patrol(moves)
    applied = patrol.set_dwell({"A": 60, "b": 5000, "ghost": 3}, 100, now=0)
    assert applied == {"a": 60, "b": 100}  # clamped to interval x 10
    assert patrol.step(now=10)
    assert patrol.snapshot().next_move_at == 10 + 60
    patrol.set_dwell({"b": 40}, 5, now=70)
    assert patrol.step(now=70) and patrol.snapshot().next_move_at == 110
    assert patrol.dwell_override(now=200) == {}
    patrol.set_dwell({"a": 2}, 100, now=200)
    assert patrol.set_dwell({"ghost": 1}, 100, now=200) == {}  # replaced by nothing
    assert patrol.dwell_override(now=201) == {}


# -------------------------------------------------------------------- API


class World:
    def __init__(self) -> None:
        self.moves: list[tuple[str, str, int]] = []

    def goto(self, camera: str, name: str, speed: int) -> bool:
        self.moves.append((camera, name, speed))
        return True


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[dict[str, Any]]:
    service, recorder = make()
    sources: list[str | None] = []

    def move(position: Position, speed: int) -> bool:
        sources.append(arbitration.source.get())
        return recorder._move(position, speed)

    service.backend.move_absolute = move
    isp: list[tuple[dict[str, Any], str | None]] = []

    def change_isp(settings: dict[str, Any]) -> bool:
        isp.append((settings, arbitration.source.get()))
        return True

    service.backend.change_isp = change_isp
    service.backend.signals = lambda: {"auto_active_age_sec": 4.2, "manual_hold_remaining_sec": 0.0}
    world = World()
    manager = SentryManager(goto_preset=world.goto, tracking_active=lambda c, h: False,
                            camera_idle=lambda c: True, gate=MovementGate(), preset_names=lambda c: ["gate", "yard"])
    mac = recorder.camera.mac  # type: ignore[union-attr]
    manager.configure(mac, config_for())
    service.backend.sentry = lambda: manager
    server = onvif.OnvifServer(service, port=0)
    server.web.assist = assist_scope.AssistLink(TOKEN, frozenset({"quality", "presets"}), "http://127.0.0.1:1")
    server.start()
    try:
        yield {"server": server, "service": service, "recorder": recorder, "sources": sources,
               "isp": isp, "manager": manager, "mac": mac}
    finally:
        manager.stop()
        server.stop()


def config_for() -> Any:
    from sentry_runtime import SentryConfig
    return SentryConfig.from_mapping({"enabled": True, "interval_sec": 3600,
                                      "points": [{"name": "gate", "dwell_sec": 30}, {"name": "yard"}]})


def bearer(server: onvif.OnvifServer, token: str = TOKEN) -> Client:
    return Client(server, {"Authorization": f"Bearer {token}"})


def test_state_carries_the_signals_and_scene(stack: dict[str, Any]) -> None:
    stack["recorder"].camera.scene = "NIGHT"
    client = Client(stack["server"])
    client.session()
    status, state = client.json("GET", "/api/v1/cameras/default/state")
    assert status == 200
    assert state["ptz"]["scene"] == "NIGHT"
    assert state["assist"] == {"enabled": True}
    assert state["assist_signals"] == {
        "scene": "NIGHT", "auto_active_age_sec": 4.2, "manual_hold_remaining_sec": 0.0,
        "patrol_hold_remaining_sec": 0.0, "lease_owner": None,
    }


def test_the_token_is_required_and_checked(stack: dict[str, Any]) -> None:
    for client in (bearer(stack["server"], "wrong"), bearer(stack["server"], "")):
        status, body = client.json("GET", "/api/v1/cameras/default/state")
        assert status == 401 and body["error"]["code"] == "bad_token"
    stack["server"].web.assist = None  # Assist switched off: no token works
    status, _ = bearer(stack["server"]).json("GET", "/api/v1/cameras/default/state")
    assert status == 401


def test_the_token_needs_no_cookie_or_csrf_and_moves_as_assist(stack: dict[str, Any]) -> None:
    client = bearer(stack["server"])
    status, _ = client.json("GET", "/api/v1/cameras/default/state")
    assert status == 200
    status, body = client.json("POST", "/api/v1/cameras/default/ptz/move",
                               {"mode": "absolute", "pan": 0.1, "tilt": 0.1})
    assert status == 202, body
    assert stack["sources"] == [ASSIST]
    status, _ = client.json("POST", "/api/v1/cameras/default/ptz/move", {"mode": "absolute"})
    assert status == 202
    assert arbitration.source.get() is None  # nothing leaks out of the request


def test_the_token_is_confined_to_its_routes(stack: dict[str, Any]) -> None:
    client = bearer(stack["server"])
    for method, path in (("GET", "/api/v1/logs"), ("POST", "/api/v1/cameras/default/ptz/recover"),
                         ("DELETE", "/api/v1/cameras/default/sentry"), ("PUT", "/api/v1/cameras/default/frigate"),
                         ("DELETE", "/api/v1/cameras/default/presets/1"), ("GET", "/api/v1/sentry"),
                         ("POST", "/api/v1/assist/check")):
        status, body = client.json(method, path, {} if method != "GET" else None)
        assert status == 403 and body["error"]["code"] == "assist_scope", path
    stack["server"].web.assist = assist_scope.AssistLink(TOKEN, frozenset({"quality"}), "http://127.0.0.1:1")
    status, body = client.json("POST", "/api/v1/cameras/default/presets", {"name": "BackPorchNight"})
    assert status == 403 and body["error"]["code"] == "assist_scope"
    status, _ = client.json("POST", "/api/v1/cameras/default/ptz/move", {"mode": "relative", "pan": 0.01})
    assert status == 202


def test_isp_from_assist_is_allowlisted(stack: dict[str, Any]) -> None:
    client = bearer(stack["server"])
    status, body = client.json("POST", "/api/v1/cameras/default/isp", {"aeTargetPercent": 45})
    assert status == 200 and body == {"acked": True}
    assert stack["isp"] == [({"aeTargetPercent": 45}, ASSIST)]
    for bad in ({"wdr": 1}, {"irLedLevel": 2}, {"surprise": 1}, {"gain": 10**9}):
        status, _ = client.json("POST", "/api/v1/cameras/default/isp", bad)
        assert status == 400
    assert len(stack["isp"]) == 1
    operator = Client(stack["server"])
    operator.session()
    status, _ = operator.json("POST", "/api/v1/cameras/default/isp", {"wdr": 1})
    assert status == 200  # the console is unchanged


def test_moves_from_assist_yield_to_the_patrol_lease(stack: dict[str, Any]) -> None:
    client = bearer(stack["server"])
    gate: MovementGate = stack["manager"].gate
    with gate.lease("sentry", stack["mac"]):
        status, body = client.json("POST", "/api/v1/cameras/default/ptz/move", {"mode": "relative", "pan": 0.01})
    assert status == 409 and body["error"]["code"] == "ptz_busy"
    assert stack["sources"] == []


def test_a_lease_owner_shows_in_the_signals(stack: dict[str, Any]) -> None:
    client = bearer(stack["server"])
    with stack["manager"].gate.lease("sentry", stack["mac"]):
        _, state = client.json("GET", "/api/v1/cameras/default/state")
    assert state["assist_signals"]["lease_owner"] == "sentry"


def test_assist_may_set_a_night_view_but_not_reshape_the_tour(stack: dict[str, Any]) -> None:
    client = bearer(stack["server"])
    _, current = client.json("GET", "/api/v1/cameras/default/sentry")
    points = [dict(p) for p in current["config"]["points"]]
    points[0]["night_preset"] = "gateNight"
    status, body = client.json("POST", "/api/v1/cameras/default/sentry", {"points": points})
    assert status == 200, body
    assert stack["manager"].config(stack["mac"]).points[0].night_preset == "gateNight"
    for bad in ({"points": points[:1]}, {"points": [{**points[0], "dwell_sec": 99}, points[1]]},
                {"enabled": False}, {"points": points, "interval_sec": 5}):
        status, _ = client.json("POST", "/api/v1/cameras/default/sentry", bad)
        assert status in (400, 403), bad
    assert stack["manager"].config(stack["mac"]).enabled


def test_patrol_hold_and_dwell_routes(stack: dict[str, Any]) -> None:
    client = bearer(stack["server"])
    status, body = client.json("POST", "/api/v1/cameras/default/sentry/hold", {"seconds": 60, "reason": "trial"})
    assert status == 200 and body["held"] is True and 59 <= body["remaining_sec"] <= 60
    _, state = client.json("GET", "/api/v1/cameras/default/state")
    assert state["assist_signals"]["patrol_hold_remaining_sec"] > 50
    status, _ = client.json("DELETE", "/api/v1/cameras/default/sentry/hold")
    assert status == 200 and stack["manager"].hold_remaining(stack["mac"]) == 0
    for bad in ({"seconds": 0}, {"seconds": 301}, {"seconds": "x"}, {}, {"seconds": 5, "reason": "x" * 65}):
        status, _ = client.json("POST", "/api/v1/cameras/default/sentry/hold", bad)
        assert status == 400, bad
    status, body = client.json("POST", "/api/v1/cameras/default/sentry/dwell",
                               {"dwell_sec": {"gate": 5000, "nowhere": 3}, "ttl_sec": 600})
    assert status == 200 and body["applied"] == {"gate": 5000.0}
    _, snapshot = client.json("GET", "/api/v1/cameras/default/sentry")
    assert snapshot["state"]["dwell_override"] == {"gate": body["applied"]["gate"]}
    assert stack["manager"].config(stack["mac"]).points[0].dwell_sec == 30  # config untouched
    status, _ = client.json("DELETE", "/api/v1/cameras/default/sentry/dwell")
    assert status == 200
    status, _ = client.json("POST", "/api/v1/cameras/default/sentry/dwell", {"dwell_sec": {"gate": 5}, "ttl_sec": 0})
    assert status == 400


def test_hold_without_a_running_patrol_is_a_harmless_no_op(stack: dict[str, Any]) -> None:
    stack["manager"].disable(stack["mac"])
    status, body = bearer(stack["server"]).json("POST", "/api/v1/cameras/default/sentry/hold", {"seconds": 5})
    assert status == 200 and body["held"] is False


# ------------------------------------------------------------------ proxy


class Upstream(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        Upstream.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                              "principal": self.headers.get("X-Osprey-Principal"), "body": body})
        if self.path.startswith("/frames/"):
            payload, kind = b"\xff\xd8jpeg", "image/jpeg"
        else:
            payload, kind = json.dumps({"ok": True}).encode(), "application/json"
        self.send_response(404 if "decisions/404" in self.path else 200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _answer  # noqa: N815

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def upstream() -> Iterator[str]:
    Upstream.seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


def test_the_console_proxy_forwards_allowed_paths_with_the_token(stack: dict[str, Any], upstream: str) -> None:
    stack["server"].web.assist = assist_scope.AssistLink(TOKEN, frozenset({"quality"}), upstream)
    console = Client(stack["server"])
    console.session()
    status, body = console.json("GET", "/api/v1/assist/decisions?status=pending&limit=5&evil=1")
    assert status == 200 and body == {"ok": True}
    status, body = console.json("POST", "/api/v1/assist/decisions/7/approve", {"note": "ok"})
    assert status == 200
    status, headers, data = console.request("GET", "/api/v1/assist/frames/a1.jpg")
    assert status == 200 and headers["content-type"] == "image/jpeg" and data.startswith(b"\xff\xd8")
    first, second, _ = Upstream.seen
    assert first["path"] == "/decisions?limit=5&status=pending"  # unknown query keys are dropped
    assert first["auth"] == f"Bearer {TOKEN}" and first["principal"] == "console"
    assert json.loads(second["body"]) == {"note": "ok"}
    status, _ = console.json("GET", "/api/v1/assist/decisions/404")
    assert status == 404  # an upstream status passes through


def test_the_console_proxy_is_closed_to_everything_else(stack: dict[str, Any], upstream: str) -> None:
    stack["server"].web.assist = assist_scope.AssistLink(TOKEN, frozenset({"quality"}), upstream)
    console = Client(stack["server"])
    console.session()
    for method, path in (("GET", "/api/v1/assist/../logs"), ("GET", "/api/v1/assist/secret"),
                         ("DELETE", "/api/v1/assist/decisions/1"), ("POST", "/api/v1/assist/frames/a.jpg"),
                         ("GET", "/api/v1/assist/frames/..%2f..%2fetc.jpg")):
        status, _ = console.json(method, path, {} if method != "GET" else None)
        assert status == 404, path
    status, _ = console.json("POST", "/api/v1/assist/check", {}, csrf=False)
    assert status == 403  # the console's CSRF rule still applies
    assert Upstream.seen == []


def test_the_console_proxy_reports_a_missing_or_dead_assist(stack: dict[str, Any]) -> None:
    console = Client(stack["server"])
    console.session()
    status, body = console.json("GET", "/api/v1/assist/status")
    assert status == 502 and body["error"]["code"] == "assist_unreachable"
    stack["server"].web.assist = None
    status, body = console.json("GET", "/api/v1/assist/status")
    assert status == 404 and body["error"]["code"] == "no_assist"
