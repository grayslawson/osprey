"""The browser app shell, /api/v1 JSON API and SSE stream. No camera.

Every test drives a real ``OnvifServer`` over loopback HTTP so routing,
authentication, CSRF and headers are exercised exactly as a browser sees them.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import logging
import socket
import time
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

import onvif
import webapi
import websecurity
from logbuffer import LogBuffer
from model import Camera, Position, Preset
from tests_support import a_camera
from test_onvif import Recorder

INGRESS = "/api/hassio_ingress/AbC123_-x"


class Client:
    """A tiny cookie-keeping HTTP client for one server."""

    def __init__(self, server: onvif.OnvifServer, headers: dict[str, str] | None = None) -> None:
        self.server = server
        self.cookie = ""
        self.csrf = ""
        self.headers = dict(headers or {})

    def request(self, method: str, path: str, body: object = None,
                headers: dict[str, str] | None = None, csrf: bool = True,
                raw: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        sent = dict(self.headers)
        if self.cookie:
            sent["Cookie"] = self.cookie
        payload: bytes | None = raw
        if body is not None:
            payload = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        if csrf and self.csrf and method not in ("GET", "HEAD"):
            sent["X-CSRF-Token"] = self.csrf
        sent.update(headers or {})
        connection.request(method, path, body=payload, headers=sent)
        response = connection.getresponse()
        data = response.read()
        result_headers: dict[str, str] = {}
        for name, value in response.getheaders():
            key = name.lower()
            result_headers[key] = f"{result_headers[key]}\n{value}" if key in result_headers else value
        connection.close()
        return response.status, result_headers, data

    def json(self, method: str, path: str, body: object = None, **kwargs: Any) -> tuple[int, Any]:
        status, _, data = self.request(method, path, body, **kwargs)
        return status, json.loads(data) if data else None

    def login(self, password: str) -> int:
        status, headers, data = self.request("POST", "/api/v1/session", {"password": password})
        if status == 200:
            self.cookie = headers["set-cookie"].split(";", 1)[0]
            self.csrf = json.loads(data)["csrf"]
        return status

    def session(self) -> dict[str, Any]:
        status, body = self.json("GET", "/api/v1/session")
        assert status == 200
        if body["csrf"]:
            self.csrf = body["csrf"]
        return dict(body)


def make(camera: Camera | None = None, *, auth: onvif.AdminAuth | None = None,
         logs: LogBuffer | None = None) -> tuple[onvif.Services, Recorder]:
    recorder = Recorder(camera if camera is not None else a_camera())
    recorder.refreshes = 0  # type: ignore[attr-defined]

    def refresh() -> bool:
        recorder.refreshes += 1  # type: ignore[attr-defined]
        return True

    backend = recorder.backend()
    backend.refresh_position = refresh
    backend.move_relative = recorder._move
    service = onvif.Services(backend, host="192.0.2.10", port=8000,
                             auth=auth or onvif.AdminAuth(allow_anonymous=True),
                             logs=logs or LogBuffer())
    return service, recorder


@pytest.fixture
def served() -> Iterator[tuple[onvif.OnvifServer, onvif.Services, Recorder]]:
    service, recorder = make()
    server = onvif.OnvifServer(service, port=0)
    server.start()
    try:
        yield server, service, recorder
    finally:
        server.stop()


def started(service: onvif.Services, **kwargs: Any) -> onvif.OnvifServer:
    server = onvif.OnvifServer(service, port=0, **kwargs)
    server.start()
    return server


# ------------------------------------------------------------------ framing


def test_sse_frame_is_one_json_line_with_optional_id() -> None:
    frame = webapi.sse_frame("ptz", {"status": "IDLE", "text": "a\nb"}, event_id=7)
    assert frame == b'event: ptz\nid: 7\ndata: {"status":"IDLE","text":"a\\nb"}\n\n'
    assert webapi.sse_comment("ping") == b": ping\n\n"
    with pytest.raises(ValueError):
        webapi.sse_frame("bad\nevent", {})
    with pytest.raises(ValueError):
        webapi.sse_frame("ptz", {"x": float("nan")})


def test_security_header_policy() -> None:
    headers = dict(websecurity.security_headers(script_hashes=("abc=",)))
    csp = headers["Content-Security-Policy"]
    assert "script-src 'self' 'sha256-abc='" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp and headers["X-Frame-Options"] == "DENY"
    framed = dict(websecurity.security_headers(framed=True))
    assert "frame-ancestors 'self'" in framed["Content-Security-Policy"]
    assert framed["X-Frame-Options"] == "SAMEORIGIN"
    assert "'unsafe-inline'" in dict(websecurity.security_headers(legacy=True))["Content-Security-Policy"]


def test_ingress_prefix_accepts_only_the_supervisor_shape() -> None:
    assert websecurity.ingress_prefix({"X-Ingress-Path": INGRESS + "/"}) == INGRESS
    for bad in ("/evil", "//evil.example/x", "/api/hassio_ingress/a b", "/api/hassio_ingress/x\r\nSet-Cookie: a"):
        assert websecurity.ingress_prefix({"X-Ingress-Path": bad}) == ""


def test_login_throttle_backs_off_and_resets() -> None:
    throttle = websecurity.LoginThrottle(free_failures=2, base_lockout=2.0, max_lockout=8.0)
    assert throttle.failure("a", now=0.0) == 0.0
    assert throttle.failure("a", now=0.0) == 0.0
    assert throttle.failure("a", now=0.0) == 2.0
    assert throttle.retry_after("a", now=1.0) == pytest.approx(1.0)
    assert throttle.failure("a", now=3.0) == 4.0
    assert throttle.failure("a", now=8.0) == 8.0
    assert throttle.failure("a", now=17.0) == 8.0  # capped
    assert throttle.retry_after("b", now=17.0) == 0.0
    throttle.success("a")
    assert throttle.retry_after("a", now=17.0) == 0.0


# ---------------------------------------------------------------- shell


def test_index_is_served_with_strict_csp_and_static_assets(served: Any) -> None:
    server, _, _ = served
    client = Client(server)
    status, headers, body = client.request("GET", "/")
    assert status == 200 and headers["content-type"].startswith("text/html")
    csp = headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp and "'sha256-" in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert b"<script" in body and b"src=\"ui/app.js\"" in body
    # Every asset reference is relative so Home Assistant ingress works.
    assert b'="/' not in body
    status, headers, js = client.request("GET", "/ui/app.js")
    assert status == 200 and headers["content-type"].startswith("text/javascript")
    status, _, _ = client.request("GET", "/ui/app.js", headers={"If-None-Match": headers["etag"]})
    assert status == 304
    for path in ("/ui/../onvif.py", "/ui/%2e%2e/onvif.py", "/ui/vendor/", "/ui/missing.js", "/ui/x.py"):
        assert client.request("GET", path)[0] == 404


def test_vendored_modules_are_served(served: Any) -> None:
    server, _, _ = served
    client = Client(server)
    _, _, body = client.request("GET", "/")
    importmap = json.loads(body.decode().split('<script type="importmap">', 1)[1].split("</script>", 1)[0])
    for target in importmap["imports"].values():
        # Import-map values must be "./"-relative (a bare path is ignored by
        # browsers); the page base decides the origin, so ingress still works.
        assert target.startswith("./ui/vendor/") and "://" not in target
        status, headers, _ = client.request("GET", "/" + target[2:])
        assert status == 200, target
        assert headers["content-type"].startswith("text/javascript")


def test_vendor_manifest_matches_the_vendored_files() -> None:
    # The offline half of scripts/verify-web-vendor.py: pins, licences and
    # checksums of the vendored browser modules must match what is shipped.
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify-web-vendor.py"
    spec = importlib.util.spec_from_file_location("verify_web_vendor", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.verify() == []


def test_new_ui_redirects_to_setup_and_is_absent_on_read_only(tmp_path: Any) -> None:
    service, recorder = make()
    readonly = onvif.Services(recorder.backend(), host="192.0.2.10", port=8001, read_only=True)
    server = started(readonly)
    try:
        client = Client(server)
        assert client.request("GET", "/")[0] == 404
        assert client.json("GET", "/api/v1/cameras")[0] == 404
        assert client.request("GET", "/ui/app.js")[0] == 404
    finally:
        server.stop()


def test_camera_route_redirects_into_the_app(served: Any) -> None:
    server, _, _ = served
    status, headers, _ = Client(server).request("GET", "/cameras/g5-ptz-aabbccddeeff/")
    assert status == 303 and headers["location"] == "/#/live?cam=g5-ptz-aabbccddeeff"
    status, headers, _ = Client(server, {"X-Ingress-Path": INGRESS}).request("GET", "/cameras/x")
    assert headers["location"] == INGRESS + "/#/live?cam=x"


# ---------------------------------------------------------------- auth


def test_console_is_locked_without_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OSPREY_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("OSPREY_ADMIN_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("OSPREY_ADMIN_AUTH", raising=False)
    auth = onvif.AdminAuth.from_environment()
    assert auth.mode == "locked"
    service, recorder = make(auth=auth)
    server = started(service)
    try:
        client = Client(server)
        assert client.session()["mode"] == "locked"
        status, body = client.json("GET", "/api/v1/cameras/default/state")
        assert status == 503 and body["error"]["code"] == "auth_not_configured"
        assert "OSPREY_ADMIN_PASSWORD_FILE" in body["error"]["hint"]
        assert client.request("GET", "/legacy")[0] == 503
        assert client.request("GET", onvif.PREVIEW_PATH + "video1")[0] == 503
        assert client.request("GET", onvif.LOGS_API_PATH)[0] == 503
        status, _, _ = client.request("POST", onvif.CONTROL_STEP_PATH, raw=b"{}",
                                      headers={"Content-Type": "application/json"})
        assert status == 503 and recorder.absolute == []
        # The app shell and public snapshot stay reachable.
        assert client.request("GET", "/")[0] == 200
        assert client.request("GET", onvif.SNAPSHOT_PATH)[0] == 200
    finally:
        server.stop()
    monkeypatch.setenv("OSPREY_ADMIN_AUTH", "disabled")
    assert onvif.AdminAuth.from_environment().mode == "disabled"


def test_password_login_logout_and_cookie_flags() -> None:
    auth = onvif.AdminAuth(onvif.AdminAuth.hash_password("correct horse", iterations=100_000))
    service, _ = make(auth=auth)
    server = started(service)
    try:
        client = Client(server)
        assert client.session() == {"mode": "password", "authenticated": False, "principal": None,
                                     "csrf": None, "ingress": False}
        status, body = client.json("GET", "/api/v1/cameras")
        assert status == 401 and body["error"]["code"] == "login_required"
        assert client.login("wrong") == 401
        status, headers, data = client.request(
            "POST", "/api/v1/session", {"password": "correct horse"},
            headers={"X-Forwarded-Proto": "https"})
        assert status == 200
        cookie = headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
        assert "Path=/" in cookie and "osprey_csrf" not in cookie
        client.cookie = cookie.split(";", 1)[0]
        client.csrf = json.loads(data)["csrf"]
        assert client.session()["authenticated"] is True
        assert client.json("GET", "/api/v1/cameras")[0] == 200
        # Logout needs the CSRF header too.
        assert client.json("DELETE", "/api/v1/session", csrf=False)[0] == 403
        status, headers, _ = client.request("DELETE", "/api/v1/session")
        assert status == 200 and "Max-Age=0" in headers["set-cookie"]
        assert client.json("GET", "/api/v1/cameras")[0] == 401
        # Plain-HTTP login does not mark the cookie Secure.
        status, headers, _ = client.request("POST", "/api/v1/session", {"password": "correct horse"})
        assert status == 200 and "Secure" not in headers["set-cookie"]
    finally:
        server.stop()


def test_legacy_login_failure_is_401_and_throttled() -> None:
    auth = onvif.AdminAuth(onvif.AdminAuth.hash_password("correct horse", iterations=100_000))
    auth.throttle = websecurity.LoginThrottle(free_failures=1, base_lockout=30.0)
    service, _ = make(auth=auth)
    server = started(service)
    try:
        client = Client(server)
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        status, _, page = client.request("POST", "/login", raw=b"password=nope", headers=form)
        assert status == 401 and b"Invalid password" in page
        assert client.request("POST", "/login", raw=b"password=nope", headers=form)[0] == 401
        # Now locked out, even with the right password.
        status, _, page = client.request("POST", "/login", raw=b"password=correct+horse", headers=form)
        assert status == 429 and b"Too many attempts" in page
        status, headers, body = client.request("POST", "/api/v1/session", {"password": "correct horse"})
        assert status == 429 and int(headers["retry-after"]) >= 1
        assert json.loads(body)["error"]["code"] == "throttled"
    finally:
        server.stop()


def test_mutations_require_the_csrf_header(served: Any) -> None:
    server, _, recorder = served
    client = Client(server)
    body = {"mode": "relative", "pan": 0.1}
    status, error = client.json("POST", "/api/v1/cameras/default/ptz/move", body)
    assert status == 403 and error["error"]["code"] == "csrf"
    client.csrf = "forged"
    assert client.json("POST", "/api/v1/cameras/default/ptz/move", body)[0] == 403
    assert recorder.absolute == []
    client.session()
    assert client.json("POST", "/api/v1/cameras/default/ptz/move", body)[0] == 202
    assert len(recorder.absolute) == 1


# ----------------------------------------------------------------- state


def test_cameras_and_state_snapshot(served: Any) -> None:
    server, service, _ = served
    client = Client(server)
    status, body = client.json("GET", "/api/v1/cameras")
    assert status == 200
    assert body["cameras"] == [{"id": "default", "name": "front gate", "model": "UVC G5 PTZ",
                                "adopted": True, "ptz": True, "status": "IDLE"}]
    status, state = client.json("GET", "/api/v1/cameras/default/state")
    assert status == 200
    assert state["ptz"]["status"] == "IDLE" and state["ptz"]["error"] is None
    assert state["ptz"]["fov_calibrated"] is True
    assert state["streams"]["video1"]["playable"] is True
    assert state["media"]["preview"] == "preview/video1"
    assert state["media"]["snapshot"] == "api/v1/cameras/default/snapshot.jpg"
    assert {c["id"] for c in state["health"]["checks"]} >= {"adoption", "ptz_channel", "frigate"}
    assert client.json("GET", "/api/v1/cameras/nope/state")[0] == 404
    status, _, image = client.request("GET", "/api/v1/cameras/default/snapshot.jpg")
    assert status == 200 and image.startswith(b"\xff\xd8")


def test_motor_timeout_is_reported_as_unknown_everywhere(served: Any) -> None:
    server, service, recorder = served
    camera = recorder.camera
    assert camera is not None
    assert camera.motion.begin(Position(pan=20000, tilt=13000, zoom=365), timeout=0.0)
    client = Client(server)
    state = client.json("GET", "/api/v1/cameras/default/state")[1]
    assert state["ptz"]["status"] == "UNKNOWN"
    assert state["ptz"]["error"] == "motor state timeout"
    motor = next(c for c in state["health"]["checks"] if c["id"] == "motor_state")
    assert motor["status"] == "bad" and "recover" in motor["actions"]
    status, _, page = client.request("GET", "/legacy")
    assert status == 200 and b"UNKNOWN: motor state timeout" in page


def test_logs_api_paging(served: Any) -> None:
    server, service, _ = served
    logger = logging.getLogger("test.webapi.logs")
    logger.addHandler(service.logs)
    try:
        for index in range(5):
            logger.warning("record %d", index)
    finally:
        logger.removeHandler(service.logs)
    client = Client(server)
    body = client.json("GET", "/api/v1/logs?limit=2&level=WARNING")[1]
    assert [e["message"] for e in body["entries"]] == ["record 4", "record 3"]
    older = client.json("GET", f"/api/v1/logs?limit=2&before={body['entries'][-1]['seq']}")[1]
    assert [e["message"] for e in older["entries"]] == ["record 2", "record 1"]
    assert client.json("GET", "/api/v1/logs?level=DEBUG")[0] == 400
    assert client.json("GET", "/api/v1/logs?limit=0")[0] == 400


# ------------------------------------------------------------------- PTZ


def test_relative_absolute_and_continuous_moves(served: Any) -> None:
    server, _, recorder = served
    client = Client(server)
    client.session()
    path = "/api/v1/cameras/default/ptz/move"
    status, body = client.json("POST", path, {"mode": "relative", "pan": 0.1, "tilt": 0.1})
    assert status == 202 and body["ok"] is True
    # Pan span 35000 -> +3500; tilt +up decreases the raw G5 tilt by 1000.
    assert recorder.absolute[-1] == Position(pan=21500, tilt=12000, zoom=365, focus=50)
    client.json("POST", path, {"mode": "absolute", "pan": -1, "tilt": 1, "zoom": 1})
    assert recorder.absolute[-1] == Position(pan=500, tilt=8000, zoom=730, focus=50)
    client.json("POST", path, {"mode": "absolute", "zoom": 0})
    assert recorder.absolute[-1] == Position(pan=18000, tilt=13000, zoom=0, focus=50)
    client.json("POST", path, {"mode": "continuous", "pan": 1})
    target = recorder.absolute[-1]
    assert target.pan > 18000 and target.tilt == 13000
    client.json("POST", path, {"mode": "relative", "pan": 1, "tilt": -1, "zoom": 1})
    assert recorder.absolute[-1] == Position(pan=35500, tilt=18000, zoom=730, focus=50)  # clamped


@pytest.mark.parametrize("body", [
    {"mode": "relative", "pan": True},
    {"mode": "relative", "pan": "0.1"},
    {"mode": "relative", "pan": 1.5},
    {"mode": "absolute", "zoom": -0.1},
    {"mode": "warp"},
    {"pan": 0.1},
    {"mode": "relative", "extra": 1},
    [1, 2],
])
def test_move_validation(served: Any, body: object) -> None:
    server, _, recorder = served
    client = Client(server)
    client.session()
    status, error = client.json("POST", "/api/v1/cameras/default/ptz/move", body)
    assert status == 400 and error["error"]["code"] == "invalid"
    assert recorder.absolute == []


def test_malformed_bodies(served: Any) -> None:
    server, _, _ = served
    client = Client(server)
    client.session()
    path = "/api/v1/cameras/default/ptz/move"
    status, _, body = client.request("POST", path, raw=b"{bad", headers={"Content-Type": "application/json"})
    assert status == 400 and json.loads(body)["error"]["code"] == "invalid_json"
    status, _, _ = client.request("POST", path, raw=b"{}", headers={"Content-Type": "text/plain"})
    assert status == 415
    status, _, _ = client.request("POST", path, raw=b"x" * (webapi.MAX_JSON_BYTES + 1),
                                  headers={"Content-Type": "application/json"})
    assert status == 413
    assert client.json("GET", "/api/v1/nothing")[0] == 404
    assert client.json("PATCH", "/api/v1/session")[0] == 405


def test_click_to_center_uses_the_field_of_view(served: Any) -> None:
    server, _, recorder = served
    client = Client(server)
    client.session()
    path = "/api/v1/cameras/default/ptz/center"
    assert client.json("POST", path, {"x": 0, "y": 0})[0] == 202
    assert recorder.absolute[-1] == Position(pan=18000, tilt=13000, zoom=365, focus=50)
    client.json("POST", path, {"x": 1, "y": 0})
    # Half the zoom-interpolated horizontal FOV, converted to motor steps.
    half_fov = (99.7 + 0.5 * (45.5 - 99.7)) / 2
    expected = 18000 + round(half_fov * 35000 / 350.0)
    assert abs(recorder.absolute[-1].pan - expected) <= 1
    client.json("POST", path, {"x": 0, "y": 1})
    assert recorder.absolute[-1].tilt < 13000  # up
    assert client.json("POST", path, {"x": 2, "y": 0})[0] == 400


def test_click_to_center_needs_calibration() -> None:
    camera = a_camera()
    camera.model = "Some Other PTZ"
    service, recorder = make(camera)
    server = started(service)
    try:
        client = Client(server)
        client.session()
        status, body = client.json("POST", "/api/v1/cameras/default/ptz/center", {"x": 0.5, "y": 0})
        assert status == 409 and body["error"]["code"] == "not_calibrated"
        # Continuous moves fall back to span fractions without calibration.
        assert client.json("POST", "/api/v1/cameras/default/ptz/move", {"mode": "continuous", "pan": -1})[0] == 202
        assert recorder.absolute[-1].pan == 18000 - round(0.03 * 35000)
    finally:
        server.stop()


def test_busy_and_unavailable_moves_are_conflicts(served: Any) -> None:
    server, service, _ = served
    client = Client(server)
    client.session()
    service.backend.move_absolute = lambda _p: False
    service.backend.move_refusal = lambda: "the camera is already moving"
    status, body = client.json("POST", "/api/v1/cameras/default/ptz/move", {"mode": "relative", "pan": 0.1})
    assert status == 409 and body["error"]["code"] == "ptz_busy"
    service.backend.move_refusal = lambda: "PTZ channel unavailable"
    status, body = client.json("POST", "/api/v1/cameras/default/ptz/center", {"x": 0.1, "y": 0})
    assert status == 409 and body["error"]["code"] == "ptz_unavailable" and body["error"]["hint"]


def test_fixed_camera_has_no_ptz() -> None:
    service, _ = make(a_camera(ptz=False))
    server = started(service)
    try:
        client = Client(server)
        client.session()
        assert client.json("GET", "/api/v1/cameras/default/state")[1]["ptz"]["supported"] is False
        status, body = client.json("POST", "/api/v1/cameras/default/ptz/stop")
        assert status == 503 and body["error"]["code"] == "no_ptz"
    finally:
        server.stop()


def test_stop_and_recover_paths(served: Any) -> None:
    server, service, recorder = served
    client = Client(server)
    client.session()
    assert client.json("POST", "/api/v1/cameras/default/ptz/stop")[0] == 202
    status, body = client.json("POST", "/api/v1/cameras/default/ptz/recover")
    assert status == 202 and "poll" in body["message"].lower()
    assert recorder.refreshes == 2
    # The watchdog contract: a backend recover_ptz() takes precedence.
    calls: list[str] = []

    def recover() -> tuple[bool, str]:
        calls.append("backend")
        return True, "channel reopened"

    service.backend.recover_ptz = recover
    status, body = client.json("POST", "/api/v1/cameras/default/ptz/recover")
    assert status == 202 and body["message"] == "channel reopened" and calls == ["backend"]
    server.web.recover_hook = lambda _services: (False, "watchdog busy")
    status, body = client.json("POST", "/api/v1/cameras/default/ptz/recover")
    assert status == 409 and body["error"]["message"] == "watchdog busy"


def test_watchdog_health_signal_is_surfaced(served: Any) -> None:
    server, service, _ = served
    service.backend.ptz_health = lambda: {
        "status": "bad", "detail": "no motor reply for 90 s", "actions": ["recover"]}
    checks = Client(server).json("GET", "/api/v1/cameras/default/health")[1]["checks"]
    watchdog = next(c for c in checks if c["id"] == "ptz_watchdog")
    assert watchdog == {"id": "ptz_watchdog", "label": "PTZ watchdog", "status": "bad",
                        "detail": "no motor reply for 90 s", "actions": ["recover"]}


# --------------------------------------------------------------- presets


def test_preset_crud_with_thumbnails(served: Any) -> None:
    server, _, recorder = served
    camera = recorder.camera
    assert camera is not None
    client = Client(server)
    client.session()
    base = "/api/v1/cameras/default/presets"
    status, body = client.json("POST", base, {"name": "  Gate "})
    assert status == 201
    preset = body["preset"]
    assert preset["name"] == "Gate" and preset["thumbnail"] is True and preset["home"] is False
    token = preset["token"]
    status, _, image = client.request("GET", f"{base}/{token}/thumbnail.jpg")
    assert status == 200 and image == recorder.image
    assert client.json("GET", base)[1]["presets"][0]["name"] == "Gate"
    assert client.json("POST", f"{base}/{token}/goto")[0] == 202
    assert recorder.preset_gotos[-1] == (token, webapi.GOTO_SPEED)
    assert client.json("PUT", f"{base}/{token}")[0] == 200
    camera.presets.pop(token)  # the Recorder backend only records removals
    camera.presets[token] = Preset(token, "Gate", Position())
    assert client.json("DELETE", f"{base}/{token}")[0] == 200
    assert recorder.presets_gone == [token]
    assert client.request("GET", f"{base}/{token}/thumbnail.jpg")[0] == 404
    assert client.json("DELETE", f"{base}/999")[0] == 404
    assert client.json("POST", base, {"name": ""})[0] == 400
    assert client.json("POST", base, {"name": "x" * 65})[0] == 400


def test_home_save_and_goto(served: Any) -> None:
    server, _, recorder = served
    client = Client(server)
    client.session()
    status, body = client.json("POST", "/api/v1/cameras/default/ptz/home")
    assert status == 404 and body["error"]["code"] == "no_home"
    assert client.json("POST", "/api/v1/cameras/default/ptz/home/save")[0] == 200
    assert client.json("POST", "/api/v1/cameras/default/ptz/home")[0] == 202
    presets = client.json("GET", "/api/v1/cameras/default/presets")[1]["presets"]
    assert presets[0]["home"] is True and presets[0]["thumbnail"] is True


def test_codec_and_frigate_settings(served: Any) -> None:
    server, service, _ = served
    client = Client(server)
    client.session()
    calls: list[tuple[str, str]] = []

    def set_encoder(track: str, codec: str) -> bool:
        calls.append((track, codec))
        return True

    service.backend.set_encoder = set_encoder
    assert client.json("PUT", "/api/v1/cameras/default/tracks/video1", {"codec": "H265"})[0] == 200
    assert calls == [("video1", "h265")]
    assert client.json("PUT", "/api/v1/cameras/default/tracks/video1", {"codec": "vp9"})[0] == 400
    assert client.json("PUT", "/api/v1/cameras/default/tracks/nope", {"codec": "h264"})[0] == 404
    status, body = client.json("PUT", "/api/v1/cameras/default/frigate", {"base_url": "ftp://x"})
    assert status == 400 and body["error"]["message"]


def test_legacy_preset_delete(served: Any) -> None:
    server, service, recorder = served
    camera = recorder.camera
    assert camera is not None
    camera.presets[3] = Preset(3, "Porch", Position())
    status, result = service.control_preset({"action": "delete", "name": "porch"})
    assert status == HTTPStatus.OK and result["token"] == 3 and recorder.presets_gone == [3]
    page = service.status_page()
    assert "data-delete-preset='Porch'" in page and "preset-table" in page


# ----------------------------------------------------------- multi-camera


def two_cameras() -> tuple[onvif.OnvifServer, Recorder, Recorder, str]:
    first, first_recorder = make()
    second_camera = a_camera()
    second_camera.mac = "02:00:00:00:00:02"
    second, second_recorder = make(second_camera)
    second.auth = first.auth
    registry = onvif.CameraRegistry(first)
    second_id = registry.register(second)
    return started(first, registry=registry), first_recorder, second_recorder, second_id


def test_camera_scoped_legacy_controls_move_the_selected_camera() -> None:
    server, first, second, second_id = two_cameras()
    try:
        client = Client(server)
        client.session()
        status, _, page = client.request("GET", f"/cameras/{second_id}/legacy")
        assert status == 200
        assert f"content='/cameras/{second_id}'".encode() in page
        assert f"src='/cameras/{second_id}/preview/video1'".encode() in page
        status, _, _ = client.request("POST", f"/cameras/{second_id}/control/step",
                                      {"axis": "pan", "direction": 1})
        assert status == 200
        assert first.absolute == [] and len(second.absolute) == 1
        status, _, image = client.request("GET", f"/cameras/{second_id}/snapshot/")
        assert status == 200 and image.startswith(b"\xff\xd8")
    finally:
        server.stop()


def test_api_lists_and_scopes_registered_cameras() -> None:
    server, first, second, second_id = two_cameras()
    try:
        client = Client(server)
        client.session()
        ids = [c["id"] for c in client.json("GET", "/api/v1/cameras")[1]["cameras"]]
        assert second_id in ids and len(ids) == 2
        state = client.json("GET", f"/api/v1/cameras/{second_id}/state")[1]
        assert state["media"]["preview"] == f"cameras/{second_id}/preview/video1"
        client.json("POST", f"/api/v1/cameras/{second_id}/ptz/move", {"mode": "relative", "pan": 0.1})
        assert first.absolute == [] and len(second.absolute) == 1
    finally:
        server.stop()


# ---------------------------------------------------------------- ingress


def test_ingress_prefix_is_honoured_for_redirects_pages_and_cookies() -> None:
    auth = onvif.AdminAuth(onvif.AdminAuth.hash_password("correct horse", iterations=100_000))
    service, _ = make(auth=auth)
    server = started(service)
    try:
        client = Client(server, {"X-Ingress-Path": INGRESS})
        status, headers, _ = client.request("GET", "/legacy")
        assert status == 303 and headers["location"] == INGRESS + "/login"
        status, headers, page = client.request("GET", "/login")
        assert f"action='{INGRESS}/login'".encode() in page
        assert headers["x-frame-options"] == "SAMEORIGIN"
        status, headers, _ = client.request("POST", "/login", raw=b"password=correct+horse",
                                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert status == 303 and headers["location"] == INGRESS + "/"
        assert f"Path={INGRESS}/" in headers["set-cookie"]
        client.cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, page = client.request("GET", "/legacy")
        assert f"<meta name='osprey-base' content='{INGRESS}'>".encode() in page
        assert client.session()["ingress"] is True
        # Not a trusted ingress peer: a session is still required.
        assert Client(server, {"X-Ingress-Path": INGRESS}).session()["authenticated"] is False
        status, headers, _ = client.request("GET", "/")
        assert "frame-ancestors 'self'" in headers["content-security-policy"]
    finally:
        server.stop()


def test_trusted_ingress_peer_is_authenticated_with_csrf() -> None:
    auth = onvif.AdminAuth(onvif.AdminAuth.hash_password("correct horse", iterations=100_000),
                           trusted_ingress_peer="127.0.0.1")
    service, recorder = make(auth=auth)
    server = started(service)
    try:
        direct = Client(server)
        assert direct.session()["authenticated"] is False
        ingress = Client(server, {"X-Ingress-Path": INGRESS})
        session = ingress.session()
        assert session["authenticated"] is True and session["principal"] == "ingress"
        assert ingress.json("POST", "/api/v1/cameras/default/ptz/move",
                            {"mode": "relative", "pan": 0.1}, csrf=False)[0] == 403
        assert ingress.json("POST", "/api/v1/cameras/default/ptz/move", {"mode": "relative", "pan": 0.1})[0] == 202
        assert len(recorder.absolute) == 1
    finally:
        server.stop()


# -------------------------------------------------------------------- SSE


class EventReader:
    def __init__(self, server: onvif.OnvifServer, path: str = "/api/v1/events",
                 headers: dict[str, str] | None = None) -> None:
        self.sock = socket.create_connection(("127.0.0.1", server.port), timeout=3)
        lines = [f"GET {path} HTTP/1.1", "Host: test", "Accept: text/event-stream"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.buffer = b""
        head = self._until(b"\r\n\r\n")
        self.status = int(head.split(b" ", 2)[1])
        self.headers = head.decode("latin-1").lower()

    def _until(self, marker: bytes) -> bytes:
        while marker not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        head, self.buffer = self.buffer.split(marker, 1)
        return head

    def event(self) -> tuple[str, Any, str | None]:
        while True:
            block = self._until(b"\n\n").decode()
            fields: dict[str, str] = {}
            for line in block.splitlines():
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(": ")
                fields[key] = value
            if "event" in fields:
                return fields["event"], json.loads(fields["data"]), fields.get("id")

    def until(self, name: str, timeout: float = 3.0) -> tuple[Any, str | None]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event, data, event_id = self.event()
            if event == name:
                return data, event_id
        raise AssertionError(f"no {name} event")

    def close(self) -> None:
        self.sock.close()


def test_sse_pushes_state_then_ptz_changes_quickly(served: Any) -> None:
    server, _, recorder = served
    camera = recorder.camera
    assert camera is not None
    reader = EventReader(server)
    try:
        assert reader.status == 200 and "content-type: text/event-stream" in reader.headers
        event, state, _ = reader.event()
        assert event == "state" and state["ptz"]["status"] == "IDLE"
        started_at = time.monotonic()
        camera.motion.update(Position(pan=19000, tilt=13000, zoom=365, focus=50), activity=0)
        ptz, _ = reader.until("ptz")
        assert time.monotonic() - started_at < 0.5
        assert ptz["raw"]["pan"] == 19000
        assert camera.motion.begin(Position(pan=1000), timeout=0.0)
        ptz, _ = reader.until("ptz")
        assert ptz["status"] == "UNKNOWN" and ptz["error"] == "motor state timeout"
    finally:
        reader.close()


def test_sse_streams_logs_presets_and_resumes(served: Any) -> None:
    server, service, recorder = served
    camera = recorder.camera
    assert camera is not None
    server.web.streams_interval = 0.2
    logger = logging.getLogger("test.webapi.sse")
    logger.addHandler(service.logs)
    try:
        logger.warning("before connect")
        reader = EventReader(server)
        try:
            reader.until("state")
            logger.error("live record")
            entry, event_id = reader.until("log")
            assert entry["message"] == "live record" and event_id == str(entry["seq"])
            camera.presets[5] = Preset(5, "Drive", Position())
            presets, _ = reader.until("preset")
            assert presets["presets"][0]["name"] == "Drive"
            streams, _ = reader.until("streams")
            assert streams["video1"]["playable"] is True
        finally:
            reader.close()
        first_seq = service.logs.entries(10)[-1]["seq"]
        resumed = EventReader(server, headers={"Last-Event-ID": str(first_seq)})
        try:
            resumed.until("state")
            entry, _ = resumed.until("log")
            assert entry["message"] == "live record"
        finally:
            resumed.close()
    finally:
        logger.removeHandler(service.logs)


def test_sse_requires_auth_and_ends_on_logout() -> None:
    auth = onvif.AdminAuth(onvif.AdminAuth.hash_password("correct horse", iterations=100_000))
    service, _ = make(auth=auth)
    server = started(service)
    server.web.heartbeat_interval = 0.2
    try:
        anonymous = EventReader(server)
        assert anonymous.status == 401
        anonymous.close()
        client = Client(server)
        assert client.login("correct horse") == 200
        reader = EventReader(server, headers={"Cookie": client.cookie})
        try:
            reader.until("state")
            assert client.request("DELETE", "/api/v1/session")[0] == 200
            ended, _ = reader.until("session", timeout=6.0)
            assert ended == {"authenticated": False}
        finally:
            reader.close()
    finally:
        server.stop()


def test_sse_client_cap(served: Any) -> None:
    server, _, _ = served
    server.web.max_sse_clients = 1
    first = EventReader(server)
    try:
        first.until("state")
        second = EventReader(server)
        assert second.status == 503
        second.close()
    finally:
        first.close()


def test_request_sockets_have_an_idle_timeout(served: Any) -> None:
    server, _, _ = served
    assert onvif._Handler.timeout is not None and onvif._Handler.timeout <= 60
