"""The operator API for the idle patrol: status, configuration, enable, remove.

The routes are exercised over real loopback HTTP so the session cookie, the
CSRF header and the error shapes are the ones a browser actually sees.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import pytest

import onvif
from sentry_manager import SentryManager
from sentry_runtime import MovementGate, SentryConfig
from test_webapi import Client, make


class World:
    """A fake camera: what moved, and whether anyone is driving it."""

    def __init__(self) -> None:
        self.moves: list[tuple[str, str, int]] = []
        self.tracking = False
        self.idle = True
        self.refuse = False

    def goto(self, camera: str, name: str, speed: int) -> bool:
        if self.refuse:
            return False
        self.moves.append((camera, name, speed))
        return True

    def tracking_active(self, camera: str, hold_sec: float) -> bool:
        return self.tracking

    def camera_idle(self, camera: str) -> bool:
        return self.idle

    def presets(self, camera: str) -> list[str]:
        return ["driveway", "gate", "home"]


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def served(world: World) -> Any:
    service, _recorder = make()
    manager = SentryManager(
        goto_preset=world.goto,
        tracking_active=world.tracking_active,
        camera_idle=world.camera_idle,
        gate=MovementGate(),
        preset_names=world.presets,
    )
    service.backend.sentry = lambda: manager
    server = onvif.OnvifServer(service, port=0)
    server.start()
    try:
        yield server, service, manager
    finally:
        manager.stop()
        server.stop()


def client_for(server: onvif.OnvifServer) -> Client:
    """A client with a session and a CSRF token.

    The fixture's console runs in the anonymous development mode, so there is
    no password to log in with; the session endpoint still hands out the
    ambient CSRF token that mutations need.
    """
    client = Client(server)
    client.session()
    return client


def test_a_camera_with_no_patrol_answers_not_found(served: Any) -> None:
    server, _service, _manager = served
    client = client_for(server)
    status, body = client.json("GET", "/api/v1/cameras/default/sentry")
    assert status == HTTPStatus.NOT_FOUND
    assert body["error"]["code"] == "no_sentry"


def test_the_overview_lists_every_camera(served: Any) -> None:
    server, _service, manager = served
    client = client_for(server)
    status, body = client.json("GET", "/api/v1/sentry")
    assert status == HTTPStatus.OK
    assert body["cameras"] == {}
    assert "movement" in body

    manager.configure("AABBCCDDEEFF", SentryConfig.from_mapping({
        "enabled": True, "points": [{"name": "driveway"}],
    }))
    status, body = client.json("GET", "/api/v1/sentry")
    assert status == HTTPStatus.OK
    assert "AABBCCDDEEFF" in body["cameras"]


def test_configuring_a_patrol_reports_it_back(served: Any) -> None:
    server, _service, _manager = served
    client = client_for(server)
    status, body = client.json("POST", "/api/v1/cameras/default/sentry", {
        "enabled": True,
        "interval_sec": 30,
        "speed": 500,
        "points": [{"name": "driveway", "dwell_sec": 20}, {"name": "gate"}],
    })
    assert status == HTTPStatus.OK
    assert body["config"]["enabled"] is True
    assert body["config"]["interval_sec"] == 30
    assert body["config"]["speed"] == 500
    assert body["config"]["points"] == [
        {"name": "driveway", "dwell_sec": 20.0},
        {"name": "gate"},
    ]
    assert body["state"]["enabled"] is True
    assert body["presets"] == ["driveway", "gate", "home"]


def test_a_configured_patrol_can_be_read_back(served: Any) -> None:
    server, _service, _manager = served
    client = client_for(server)
    client.json("POST", "/api/v1/cameras/default/sentry", {
        "enabled": False, "points": [{"name": "gate"}],
    })
    status, body = client.json("GET", "/api/v1/cameras/default/sentry")
    assert status == HTTPStatus.OK
    assert body["config"]["enabled"] is False
    assert body["state"]["state"] == "disabled"


def test_disabling_keeps_the_points(served: Any) -> None:
    server, _service, _manager = served
    client = client_for(server)
    client.json("POST", "/api/v1/cameras/default/sentry", {
        "enabled": True, "points": [{"name": "gate"}],
    })
    status, body = client.json("POST", "/api/v1/cameras/default/sentry", {"enabled": False})
    assert status == HTTPStatus.OK
    assert body["config"]["enabled"] is False
    assert body["config"]["points"] == [{"name": "gate"}]


def test_removing_a_patrol_forgets_it(served: Any) -> None:
    server, _service, _manager = served
    client = client_for(server)
    client.json("POST", "/api/v1/cameras/default/sentry", {
        "enabled": True, "points": [{"name": "gate"}],
    })
    status, body = client.json("DELETE", "/api/v1/cameras/default/sentry")
    assert status == HTTPStatus.OK
    assert body["ok"] is True
    status, body = client.json("GET", "/api/v1/cameras/default/sentry")
    assert status == HTTPStatus.NOT_FOUND
    assert body["error"]["code"] == "no_sentry"


@pytest.mark.parametrize("body", [
    {"interval_sec": 0},
    {"interval_sec": "nope"},
    {"speed": 0},
    {"speed": "fast"},
    {"tracking_hold_sec": 0},
    {"points": "driveway"},
    {"points": [{"dwell_sec": 5}]},
    {"points": [{"name": "a"}, {"name": "A"}]},
    {"points": [{"name": "a", "bogus": 1}]},
    {"unknown_field": 1},
])
def test_a_malformed_patrol_is_rejected(served: Any, body: dict[str, object]) -> None:
    server, _service, _manager = served
    client = client_for(server)
    status, payload = client.json("POST", "/api/v1/cameras/default/sentry", body)
    assert status == HTTPStatus.BAD_REQUEST
    assert payload["error"]["code"] == "invalid"


def test_a_patrol_needs_the_csrf_header(served: Any) -> None:
    server, _service, _manager = served
    client = client_for(server)
    status, payload = client.json(
        "POST", "/api/v1/cameras/default/sentry",
        {"enabled": True, "points": [{"name": "gate"}]}, csrf=False,
    )
    assert status == HTTPStatus.FORBIDDEN
    assert payload["error"]["code"] in ("csrf", "forbidden")


def test_a_forged_csrf_token_is_refused(served: Any) -> None:
    server, _service, _manager = served
    client = client_for(server)
    client.csrf = "forged"
    status, payload = client.json(
        "POST", "/api/v1/cameras/default/sentry",
        {"enabled": True, "points": [{"name": "gate"}]},
    )
    assert status == HTTPStatus.FORBIDDEN
    assert payload["error"]["code"] == "csrf"
