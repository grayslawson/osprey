"""Browser first-run setup writes safe, restartable deployment state."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
from typing import cast

import onvif
import pytest
import setup_wizard
from setup_wizard import SetupError, SetupManager, load_environment
from tests_support import a_camera, assert_no_secret


def test_setup_manager_generates_config_secrets_without_plaintext_admin(tmp_path: Path) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="setup-token")
    result = manager.save(
        {
            "host": "osprey.example.test",
            "bind": "0.0.0.0",
            "name": "Driveway PTZ",
            "mac": "AA:BB:CC:DD:EE:FF",
            "ip": "192.0.2.20",
            "frigate_url": "http://frigate.example.test:5000",
            "admin_password": "correct horse battery staple",
        },
        "setup-token",
    )
    assert result["ok"] is True
    credentials = cast(dict[str, str], result["generated_credentials"])
    assert set(credentials) == {
        "rtsp_username", "rtsp_password", "onvif_username", "onvif_password",
    }
    config_text = (tmp_path / "osprey.json").read_text()
    assert_no_secret(config_text, "correct horse battery staple", "correct horse")
    assert json.loads(config_text)["cameras"][0]["mac"] == "AABBCCDDEEFF"
    assert (tmp_path / "osprey-secrets.env").stat().st_mode & 0o077 == 0
    assert (tmp_path / "rtsp-password").stat().st_mode & 0o077 == 0
    assert manager.pending is False
    try:
        manager.save({}, "setup-token")
    except SetupError as exc:
        assert "already complete" in str(exc)
    else:
        raise AssertionError("completed setup accepted a second write")


def test_setup_http_route_requires_token_and_returns_credentials(tmp_path: Path) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="setup-token")
    backend = onvif.Backend(
        camera=lambda: a_camera(),
        stream_uri=lambda token: f"rtsp://127.0.0.1/{token}",
        snapshot_uri=lambda token: f"http://127.0.0.1/snapshot/{token}",
    )
    services = onvif.Services(backend, "127.0.0.1", port=0, setup=manager)
    server = onvif.OnvifServer(services, port=0, bind_host="127.0.0.1")
    server.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
        client.request("GET", "/")
        response = client.getresponse()
        assert response.status == 303
        assert response.getheader("Location") == "/setup"
        response.read()

        # The page is a static file under the strict app policy: no inline script.
        client.request("GET", "/setup?token=setup-token")
        response = client.getresponse()
        page = response.read().decode()
        assert response.status == 200
        assert "'unsafe-inline'" not in response.getheader("Content-Security-Policy", "")
        assert response.getheader("Referrer-Policy") == "no-referrer"
        assert '<script src="ui/setup.js"></script>' in page and "setup-token" not in page

        client.request("GET", "/api/setup", headers={"X-Setup-Token": "wrong"})
        response = client.getresponse()
        assert response.status == 403
        response.read()
        client.request("GET", "/api/setup", headers={"X-Setup-Token": "setup-token"})
        response = client.getresponse()
        assert response.status == 200
        status = json.loads(response.read())
        assert status["pending"] is True and status["ingress"] is False

        payload = json.dumps({"setup_token": "wrong", "host": "x", "bind": "0.0.0.0"})
        client.request("POST", "/api/setup", payload, {"Content-Type": "application/json"})
        response = client.getresponse()
        assert response.status == 400
        assert "setup token" in response.read().decode().lower()

        manager.save(
            {
                "host": "127.0.0.1",
                "bind": "127.0.0.1",
                "name": "Driveway",
                "mac": "AA:BB:CC:DD:EE:FF",
                "ip": "192.0.2.20",
                "frigate_url": "http://frigate.example.test:5000",
                "admin_password": "correct horse battery staple",
            },
            "setup-token",
        )
        client.request("GET", "/setup")
        response = client.getresponse()
        assert response.status == 404
        response.read()

        client.request("POST", "/api/setup", "{}", {"Content-Type": "application/json"})
        response = client.getresponse()
        assert response.status == 404
        response.read()
    finally:
        server.stop()


def test_generated_environment_is_loaded_without_overriding_explicit_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("OSPREY_ADMIN_PASSWORD_HASH=generated\n# ignored\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.delenv("OSPREY_ADMIN_PASSWORD_HASH", raising=False)
    load_environment(path)
    assert os.environ["OSPREY_ADMIN_PASSWORD_HASH"] == "generated"
    monkeypatch.setenv("OSPREY_ADMIN_PASSWORD_HASH", "explicit")
    load_environment(path)
    assert os.environ["OSPREY_ADMIN_PASSWORD_HASH"] == "explicit"
    monkeypatch.delenv("OSPREY_ADMIN_PASSWORD_HASH")
    path.chmod(0o644)
    load_environment(path)
    assert "OSPREY_ADMIN_PASSWORD_HASH" not in os.environ


def test_setup_commit_rolls_back_all_artifacts_when_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="setup-token")
    original_replace = os.replace
    calls = 0

    def fail_once(
        source: str | bytes | os.PathLike[str], target: str | bytes | os.PathLike[str]
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interrupted commit")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_once)
    with pytest.raises(OSError, match="simulated interrupted commit"):
        manager.save(
            {
                "host": "osprey.example.test",
                "bind": "0.0.0.0",
                "name": "Driveway PTZ",
                "mac": "AA:BB:CC:DD:EE:FF",
                "ip": "192.0.2.20",
                "frigate_url": "http://frigate.example.test:5000",
                "admin_password": "correct horse battery staple",
            },
            "setup-token",
        )

    assert manager.pending
    assert not list(tmp_path.glob("*"))


def test_non_ascii_setup_token_is_refused_not_a_crash(tmp_path: Path) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="setup-token")
    with pytest.raises(SetupError):
        manager.save({}, "é")


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_camera_ip_is_optional(tmp_path: Path, blank: object) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="t")
    manager.save(
        {"host": "osprey.example.test", "name": "Cam", "mac": "AABBCCDDEEFF", "ip": blank,
         "frigate_url": "http://frigate.example.test:5000", "admin_password": "correct horse"},
        "t",
    )
    assert "ip" not in json.loads((tmp_path / "osprey.json").read_text())["cameras"][0]


def test_a_malformed_camera_ip_is_still_rejected(tmp_path: Path) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="t")
    with pytest.raises(SetupError, match="valid IPv4 or IPv6"):
        manager.save(
            {"host": "osprey.example.test", "name": "Cam", "mac": "AABBCCDDEEFF", "ip": "not-an-ip",
             "frigate_url": "http://frigate.example.test:5000", "admin_password": "correct horse"},
            "t",
        )


SAVE = {
    "host": "192.0.2.10", "name": "Cam", "mac": "AABBCCDDEEFF",
    "admin_password": "correct horse",
}


def test_frigate_is_optional_and_writes_no_frigate_file(tmp_path: Path) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="t")
    manager.save(dict(SAVE), "t")
    assert "frigate" not in json.loads((tmp_path / "osprey.json").read_text())
    assert not (tmp_path / "frigate.json").exists()
    assert f"OSPREY_FRIGATE_CONFIG_FILE={tmp_path / 'frigate.json'}" in (tmp_path / "osprey-secrets.env").read_text()


def test_the_token_expires_and_a_new_link_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="old")
    manager.url = "http://192.0.2.10:8000/setup"
    later = manager.issued + setup_wizard.TOKEN_TTL_SEC + 1
    monkeypatch.setattr("setup_wizard.time.monotonic", lambda: later)
    with pytest.raises(SetupError, match="expired"):
        manager.save(dict(SAVE), "old")
    assert manager.token != "old"
    assert f"http://192.0.2.10:8000/setup?token={manager.token}" in caplog.text
    manager.save(dict(SAVE), manager.token)  # the new link works once
    assert manager.pending is False


def test_trusted_ingress_needs_no_token_but_setup_is_still_single_use(tmp_path: Path) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="t")
    manager.save(dict(SAVE), "", trusted=True)
    with pytest.raises(SetupError, match="already complete"):
        manager.save(dict(SAVE), "", trusted=True)


def test_saving_clears_the_pending_marker_and_asks_for_a_restart(tmp_path: Path) -> None:
    (tmp_path / setup_wizard.PENDING_MARKER).touch()
    calls: list[str] = []
    manager = SetupManager(tmp_path / "osprey.json", token="t")
    manager.on_complete = lambda: calls.append("restart")
    result = manager.save(dict(SAVE), "t")
    assert calls == ["restart"]
    assert result["restarting"] is True and result["restart_required"] is False
    assert not (tmp_path / setup_wizard.PENDING_MARKER).exists()


def test_the_startup_banner_carries_a_clickable_link(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="abc")
    manager.url = "http://192.0.2.10:8000/setup"
    manager.announce()
    assert "http://192.0.2.10:8000/setup?token=abc" in caplog.text


def test_lan_detection_uses_the_default_route_source(monkeypatch: pytest.MonkeyPatch) -> None:
    class Probe:
        def __init__(self, *args: object) -> None:
            pass

        def __enter__(self) -> "Probe":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def connect(self, address: tuple[str, int]) -> None:
            assert address[0] == "192.0.2.1"  # a documentation address: nothing is sent

        def getsockname(self) -> tuple[str, int]:
            return ("198.51.100.7", 40000)

    monkeypatch.setattr("setup_wizard.socket.socket", Probe)
    assert setup_wizard.detect_lan_ip() == "198.51.100.7"

    def no_route(self: Probe, address: tuple[str, int]) -> None:
        raise OSError("Network is unreachable")

    monkeypatch.setattr(Probe, "connect", no_route)
    assert setup_wizard.detect_lan_ip() is None


def test_ingress_requests_reach_setup_without_a_token(tmp_path: Path) -> None:
    manager = SetupManager(tmp_path / "osprey.json", token="setup-token")
    backend = onvif.Backend(camera=lambda: a_camera(), stream_uri=lambda t: "", snapshot_uri=lambda t: "")
    services = onvif.Services(
        backend, "127.0.0.1", port=0, setup=manager,
        auth=onvif.AdminAuth(None, trusted_ingress_peer="127.0.0.1"),
    )
    server = onvif.OnvifServer(services, port=0, bind_host="127.0.0.1")
    server.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
        client.request("GET", "/api/setup", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
        response = client.getresponse()
        assert response.status == 200 and json.loads(response.read())["ingress"] is True
        client.request("GET", "/api/setup")
        response = client.getresponse()
        assert response.status == 403
        response.read()
    finally:
        server.stop()
