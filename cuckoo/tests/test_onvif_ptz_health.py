"""Operator-facing PTZ honesty: console state, Recover PTZ, /healthz, Home."""

from __future__ import annotations

import dataclasses
import http.client
import json
import re
from http import HTTPStatus

import onvif
from model import Position, Preset
from tests_support import a_camera
from test_onvif import Recorder, ask, text_of


class HealthRecorder(Recorder):
    def __init__(self, healthy: bool = True, recover_ok: bool = True) -> None:
        super().__init__(a_camera())
        self.recoveries = 0
        self.healthy = healthy
        self.recover_ok = recover_ok

    def backend(self) -> onvif.Backend:
        return dataclasses.replace(
            super().backend(), recover_ptz=self._recover, ptz_health=self._health
        )

    def _recover(self) -> bool:
        self.recoveries += 1
        return self.recover_ok

    def _health(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "state": "IDLE" if self.healthy else "UNKNOWN",
            "error": None if self.healthy else "PTZ not responding; recovering channel",
            "last_motor_reply_age_sec": 1.5,
            "watchdog_recycles": 0 if self.healthy else 2,
        }


def _card(page: str) -> str:
    match = re.search(r"<div class='metric' id='ptz-state-card'>(.*?)</div>", page)
    assert match is not None
    return match.group(1)


def test_console_headline_shows_unknown_and_the_error_when_ptz_is_down() -> None:
    recorder = HealthRecorder()
    assert recorder.camera is not None
    recorder.camera.motion.disconnect("PTZ not responding; recovering channel")
    service = onvif.Services(recorder.backend(), host="10.0.0.1", port=8000)

    card = _card(service.status_page())

    assert "UNKNOWN" in card and "IDLE" not in card
    assert "PTZ not responding; recovering channel" in card
    assert "alert-danger" in card


def test_console_headline_shows_a_motor_timeout_rather_than_idle() -> None:
    recorder = HealthRecorder()
    assert recorder.camera is not None
    motion = recorder.camera.motion
    assert motion.begin(Position(pan=1, tilt=1, zoom=1), timeout=0.0)
    service = onvif.Services(recorder.backend(), host="10.0.0.1", port=8000)

    page = service.status_page()

    assert "UNKNOWN" in _card(page)
    assert "motor state timeout" in _card(page)
    assert "id='recover-ptz'" in page


def test_console_headline_is_idle_when_healthy() -> None:
    service = onvif.Services(HealthRecorder().backend(), host="10.0.0.1", port=8000)
    card = _card(service.status_page())
    assert "IDLE" in card and "alert-danger" not in card


def test_recover_control_reports_acceptance_and_refusal() -> None:
    recorder = HealthRecorder()
    service = onvif.Services(recorder.backend(), host="10.0.0.1", port=8000)
    status, body = service.control_recover()
    assert status == HTTPStatus.ACCEPTED and body["ok"] is True
    assert recorder.recoveries == 1

    recorder.recover_ok = False
    status, body = service.control_recover()
    assert status == HTTPStatus.CONFLICT and "management channel" in str(body["error"])


def test_recover_endpoint_is_csrf_protected_and_healthz_is_public() -> None:
    recorder = HealthRecorder(healthy=False)
    auth = onvif.AdminAuth(onvif.AdminAuth.hash_password("correct horse"))
    service = onvif.Services(recorder.backend(), host="127.0.0.1", port=8000, auth=auth)
    server = onvif.OnvifServer(service, port=0)
    server.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        connection.request("GET", onvif.HEALTH_PATH)
        response = connection.getresponse()
        health = json.loads(response.read())
        assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
        assert health["status"] == "degraded"
        assert health["ptz"]["state"] == "UNKNOWN"
        assert health["ptz"]["watchdog_recycles"] == 2
        assert "AABBCCDDEEFF" not in json.dumps(health).upper(), "no MAC on a public endpoint"

        connection.request("POST", onvif.CONTROL_RECOVER_PATH, body="{}",
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        assert response.status == HTTPStatus.UNAUTHORIZED
        connection.close()  # the refusal does not drain the body; start clean

        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        connection.request("POST", "/login", body="password=correct+horse",
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        cookies = response.getheaders()
        response.read()
        session_cookie = next(value.split(";", 1)[0] for name, value in cookies
                              if name.lower() == "set-cookie"
                              and value.startswith(f"{onvif.SESSION_COOKIE}="))
        # The CSRF token is not a cookie: the session endpoint returns it to the
        # authenticated browser, which then sends it as X-CSRF-Token.
        assert not any(value.startswith("osprey_csrf=") for name, value in cookies)
        connection.request("GET", "/api/v1/session", headers={"Cookie": session_cookie})
        response = connection.getresponse()
        csrf = json.loads(response.read())["csrf"]
        assert response.status == HTTPStatus.OK and csrf

        connection.request("POST", onvif.CONTROL_RECOVER_PATH, body="{}",
                           headers={"Content-Type": "application/json", "Cookie": session_cookie})
        response = connection.getresponse()
        response.read()
        assert response.status == HTTPStatus.FORBIDDEN
        assert recorder.recoveries == 0

        connection.request("POST", onvif.CONTROL_RECOVER_PATH, body="{}",
                           headers={"Content-Type": "application/json", "Cookie": session_cookie,
                                    "X-CSRF-Token": csrf})
        response = connection.getresponse()
        response.read()
        assert response.status == HTTPStatus.ACCEPTED
        assert recorder.recoveries == 1
        connection.close()
    finally:
        server.stop()


def test_healthz_is_ok_when_ptz_is_healthy_and_waiting_before_adoption() -> None:
    service = onvif.Services(HealthRecorder().backend(), host="10.0.0.1", port=8000)
    status, body = service.health()
    assert status == HTTPStatus.OK and body["status"] == "ok"

    empty = Recorder(None)
    service = onvif.Services(empty.backend(), host="10.0.0.1", port=8000)
    status, body = service.health()
    assert status == HTTPStatus.SERVICE_UNAVAILABLE and body["status"] == "waiting"


def test_home_position_is_supported_through_the_home_preset() -> None:
    recorder = HealthRecorder()
    assert recorder.camera is not None
    service = onvif.Services(recorder.backend(), host="10.0.0.1", port=8000)

    assert text_of(ask(service, "GetNodes"), "HomeSupported") == "true"
    assert "Fault" in ask(service, "GotoHomePosition")

    assert "SetHomePositionResponse" in ask(service, "SetHomePosition")
    home = next(p for p in recorder.camera.presets.values() if p.name == "home")
    assert "GotoHomePositionResponse" in ask(service, "GotoHomePosition")
    assert recorder.preset_gotos[-1][0] == home.index


def test_goto_home_matches_the_home_preset_case_insensitively() -> None:
    recorder = HealthRecorder()
    assert recorder.camera is not None
    recorder.camera.presets[9] = Preset(9, "Home", Position())
    service = onvif.Services(recorder.backend(), host="10.0.0.1", port=8000)
    assert "GotoHomePositionResponse" in ask(service, "GotoHomePosition")
    assert recorder.preset_gotos == [(9, 1000)]
