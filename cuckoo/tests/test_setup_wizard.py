"""Browser first-run setup writes safe, restartable deployment state."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
from typing import cast

import onvif
import pytest
from setup_wizard import SetupError, SetupManager, load_environment
from tests_support import a_camera


def test_setup_manager_generates_config_secrets_without_plaintext_admin(tmp_path: Path) -> None:
    manager = SetupManager(tmp_path / "cuckoo.json", token="setup-token")
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
    config_text = (tmp_path / "cuckoo.json").read_text()
    assert "correct horse" not in config_text
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
    manager = SetupManager(tmp_path / "cuckoo.json", token="setup-token")
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

        payload = json.dumps({"setup_token": "wrong", "host": "x", "bind": "0.0.0.0"})
        client.request("POST", "/api/setup", payload, {"Content-Type": "application/json"})
        response = client.getresponse()
        assert response.status == 400
        assert "setup token" in response.read().decode().lower()
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
