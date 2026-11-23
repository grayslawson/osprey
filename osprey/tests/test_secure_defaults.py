"""Secure defaults for new installs, legacy behaviour for existing ones."""

from __future__ import annotations

import logging
import socket
import time
from pathlib import Path

import pytest

import main
import media
import secure_defaults
from test_media import stream_bytes


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OSPREY_SECURITY", "OSPREY_ONVIF_AUTH", "OSPREY_RTSP_AUTH", "OSPREY_ONVIF_USERNAME",
                 "OSPREY_ONVIF_PASSWORD", "OSPREY_ONVIF_PASSWORD_FILE", "OSPREY_RTSP_USERNAME",
                 "OSPREY_RTSP_PASSWORD", "OSPREY_RTSP_PASSWORD_FILE", "OSPREY_ONVIF_USERS_FILE"):
        monkeypatch.delenv(name, raising=False)


def _build(options: main.Options) -> main.Stack:
    stack = main.build(options)
    stack.start_servers()  # stop() waits for serve_forever, so a stack must be started
    return stack


def _options(state: Path, security: str) -> main.Options:
    return main.Options(
        host="127.0.0.1", bind_host="127.0.0.1", cert=state / "osprey.pem", security=security,
        control_port=0, ingest_port=0, snapshot_port=0, rtsp_port=0, onvif_port=0,
        discovery_port=0, announce=False,
    )


def test_fresh_state_dir_is_secure_and_stays_so(fresh: None, tmp_path: Path) -> None:
    cert = tmp_path / "osprey.pem"
    assert secure_defaults.resolve(tmp_path, (cert, tmp_path / "osprey.json")) == "secure"
    cert.write_text("x")  # the first run created it; that must not flip the decision
    assert secure_defaults.resolve(tmp_path, (cert,)) == "secure"


def test_existing_install_keeps_legacy_and_warns(
    fresh: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "osprey.pem").write_text("x")
    with caplog.at_level(logging.WARNING):
        assert secure_defaults.resolve(tmp_path, (tmp_path / "osprey.pem",)) == "legacy"
    assert "recommended" in caplog.text.lower()
    assert (tmp_path / secure_defaults.MARKER).read_text().strip() == "legacy"
    assert secure_defaults.resolve(tmp_path, ()) == "legacy"  # remembered


def test_environment_overrides_the_marker(fresh: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / secure_defaults.MARKER).write_text("legacy\n")
    monkeypatch.setenv("OSPREY_SECURITY", "secure")
    assert secure_defaults.resolve(tmp_path, ()) == "secure"


def test_credentials_are_generated_once_0600(
    fresh: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        first = secure_defaults.credential(tmp_path, "onvif")
        again = secure_defaults.credential(tmp_path, "onvif")
    assert first == again and len(first) >= 24
    assert caplog.text.count(first) == 1  # shown in the log exactly once
    assert (tmp_path / "onvif-password").stat().st_mode & 0o777 == 0o600


def test_secure_stack_requires_onvif_and_rtsp_auth(fresh: None, tmp_path: Path) -> None:
    stack = _build(_options(tmp_path, "secure"))
    try:
        assert stack.stream.auth is not None
        assert not stack.services.onvif_auth.authenticate(b"<Envelope/>").ok
        # Stack restarts reuse the stored credential.
        assert (tmp_path / "onvif-password").exists() and (tmp_path / "rtsp-password").exists()
    finally:
        stack.stop()


def test_legacy_stack_keeps_anonymous_onvif_and_rtsp(fresh: None, tmp_path: Path) -> None:
    stack = _build(_options(tmp_path, "legacy"))
    try:
        assert stack.services.onvif_auth.authenticate(b"<Envelope/>").ok
        assert stack.stream.auth is None
        assert stack.ingest.allowed_peers is None and stack.uploads.allowed_peers is None
        assert not (tmp_path / "onvif-password").exists()
    finally:
        stack.stop()


def test_explicit_opt_out_keeps_anonymous_even_when_secure(
    fresh: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSPREY_ONVIF_AUTH", "disabled")
    monkeypatch.setenv("OSPREY_RTSP_AUTH", "disabled")
    stack = _build(_options(tmp_path, "secure"))
    try:
        assert stack.services.onvif_auth.authenticate(b"<Envelope/>").ok
        assert stack.stream.auth is None
    finally:
        stack.stop()


def test_secure_ingest_accepts_only_the_adopted_camera(fresh: None, tmp_path: Path) -> None:
    stack = _build(_options(tmp_path, "secure"))
    ingest = stack.ingest
    try:
        assert ingest.allowed_peers == set()  # enforced while empty
        port = int(ingest.server_address[1])

        def push() -> list[str]:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(stream_bytes("video1"))
            time.sleep(0.1)
            return stack.hub.names()

        assert push() == []
        assert stack.controller.learned_peers is ingest.allowed_peers  # adoption feeds the same set
        ingest.allowed_peers.add("127.0.0.1")  # what the controller does on a camera handshake
        assert push() == ["video1"]
    finally:
        stack.stop()


def test_configured_camera_ip_is_the_only_allowed_peer(fresh: None, tmp_path: Path) -> None:
    options = _options(tmp_path, "secure")
    options.cameras = ({"mac": "001122334455", "ip": "192.0.2.9"},)
    stack = _build(options)
    try:
        assert stack.ingest.allowed_peers == {"192.0.2.9"}
        assert stack.controller.learned_peers is None  # a configured IP is never widened
    finally:
        stack.stop()
