"""The wizard's steps after the save: adopt against a fake camera, behind admin auth."""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import manage
import onvif
from onboarding import Onboarding, OnboardingError
from setup_wizard import SetupManager
from test_manage import FakeCamera, a_camera
from tests_support import a_camera as a_model


@pytest.fixture
def reset_camera(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A factory-fresh camera's management API: ubnt / ubnt."""
    monkeypatch.setattr(FakeCamera, "username", "ubnt")
    monkeypatch.setattr(FakeCamera, "password", "ubnt")
    FakeCamera.received = {}
    server, port = a_camera(tmp_path)
    yield port
    server.shutdown()


def test_adopt_points_a_reset_camera_at_osprey(reset_camera: int) -> None:
    steps = Onboarding(host="192.0.2.10", control_port=7442, camera_port=reset_camera)
    result = steps.adopt({"ip": "127.0.0.1"})
    assert result["ok"] is True
    assert FakeCamera.received == manage.manage_payload("192.0.2.10", "osprey", 7442)


def test_a_wrong_camera_password_is_reported_not_raised(reset_camera: int) -> None:
    steps = Onboarding(host="192.0.2.10", control_port=7442, camera_port=reset_camera)
    with pytest.raises(OnboardingError, match="rejected the credential") as caught:
        steps.adopt({"ip": "127.0.0.1", "username": "ubnt", "password": "nope"})
    assert caught.value.status == 502


@pytest.mark.parametrize("body, message", [
    ({"ip": "camera.lan"}, "valid IP address"),
    ({"ip": "127.0.0.1", "password": 5}, "password must be text"),
    ({"ip": "127.0.0.1", "username": "u" * 65}, "username must be text"),
    ([], "must be an object"),
])
def test_adopt_validates_its_input(body: object, message: str) -> None:
    with pytest.raises(OnboardingError, match=message):
        Onboarding(host="192.0.2.10", control_port=7442).adopt(body)


def test_status_lets_the_wizard_resume() -> None:
    steps = Onboarding(host="192.0.2.10", control_port=7442,
                       cameras=({"mac": "AABBCCDDEEFF", "ip": "192.0.2.20", "name": "Cam", "extra": 1},))
    assert steps.status("http://frigate.lan:5000") == {
        "host": "192.0.2.10",
        "cameras": [{"mac": "AABBCCDDEEFF", "ip": "192.0.2.20", "name": "Cam"}],
        "frigate_url": "http://frigate.lan:5000",
    }


def _server(tmp_path: Path, auth: onvif.AdminAuth, setup: SetupManager | None = None) -> onvif.OnvifServer:
    backend = onvif.Backend(camera=lambda: a_model(), stream_uri=lambda t: "", snapshot_uri=lambda t: "")
    services = onvif.Services(backend, "127.0.0.1", port=0, auth=auth, setup=setup)
    server = onvif.OnvifServer(services, port=0, bind_host="127.0.0.1")
    server.web.onboarding = Onboarding(host="127.0.0.1", control_port=7442)
    server.start()
    return server


def test_post_save_steps_need_the_admin_session_and_csrf(tmp_path: Path) -> None:
    auth = onvif.AdminAuth(onvif.AdminAuth.hash_password("correct horse", iterations=100_000))
    server = _server(tmp_path, auth)
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        client.request("GET", "/api/v1/setup/status")
        response = client.getresponse()
        assert response.status == 401
        response.read()

        client.request("POST", "/api/v1/session", json.dumps({"password": "correct horse"}),
                       {"Content-Type": "application/json"})
        response = client.getresponse()
        login = json.loads(response.read())
        cookie = response.getheader("Set-Cookie", "").split(";", 1)[0]

        client.request("POST", "/api/v1/setup/adopt", json.dumps({"ip": "192.0.2.20"}),
                       {"Content-Type": "application/json", "Cookie": cookie})
        response = client.getresponse()
        assert response.status == 403  # no CSRF token
        response.read()

        client.request("POST", "/api/v1/setup/adopt", json.dumps({"ip": "not an ip"}),
                       {"Content-Type": "application/json", "Cookie": cookie, "X-CSRF-Token": login["csrf"]})
        response = client.getresponse()
        assert response.status == 400 and "valid IP address" in response.read().decode()

        # An admin can reopen the wizard after the save to finish the steps.
        client.request("GET", "/setup", headers={"Cookie": cookie})
        response = client.getresponse()
        assert response.status == 200
        response.read()
    finally:
        server.stop()


def test_discovery_before_the_save_needs_the_setup_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("camera_finder.scan", lambda: [{"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.0.2.20"}])
    monkeypatch.setattr("camera_finder.probe", lambda ip: None)
    server = _server(tmp_path, onvif.AdminAuth(), SetupManager(tmp_path / "osprey.json", token="t"))
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        for token, body, status in (
            ("wrong", "{}", 400),
            ("t", "{}", 200),
            ("t", json.dumps({"ip": "192.0.2.99"}), 200),
            ("t", json.dumps({"ip": "nope"}), 400),
        ):
            client.request("POST", "/api/setup/discover", body,
                           {"Content-Type": "application/json", "X-Setup-Token": token})
            response = client.getresponse()
            data = json.loads(response.read())
            assert response.status == status, data
            if token == "t" and status == 200:
                assert data["cameras"] == ([] if "192.0.2.99" in body else [{"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.0.2.20"}])
    finally:
        server.stop()


def test_osprey_adopt_takes_its_defaults_from_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "osprey.json"
    config.write_text(json.dumps({"host": "192.0.2.10", "ports": {"control": 7443}}))
    monkeypatch.setenv("OSPREY_CONFIG_FILE", str(config))
    monkeypatch.delenv("OSPREY_HOST", raising=False)
    monkeypatch.setenv("OSPREY_CAMERA_PASSWORD", "ubnt")
    seen: list[tuple[object, ...]] = []

    def record(*args: object) -> dict[str, object]:
        seen.append(args)
        return {}

    monkeypatch.setattr(manage, "take_custody", record)
    assert manage.main(["--camera", "192.0.2.20"]) == 0
    assert seen == [("192.0.2.20", "ubnt", "ubnt", "192.0.2.10", "osprey", 7443, 443)]
