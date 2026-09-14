"""Regression tests for optional ONVIF WS-Security authentication."""

from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path

import onvif
import pytest


def _request(
    *,
    username: str = "frigate",
    password: str = "correct horse",
    nonce: bytes = b"0123456789abcdef",
    created: str | None = None,
) -> bytes:
    timestamp = created or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    digest = base64.b64encode(
        hashlib.sha1(nonce + timestamp.encode() + password.encode()).digest()
    ).decode()
    return onvif.envelope(
        f"""
        <s:Header>
          <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <wsse:UsernameToken>
              <wsse:Username>{username}</wsse:Username>
              <wsse:Password Type="...#PasswordDigest">{digest}</wsse:Password>
              <wsse:Nonce EncodingType="...#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce>
              <wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{timestamp}</wsu:Created>
            </wsse:UsernameToken>
          </wsse:Security>
        </s:Header>
        <s:Body><tds:GetDeviceInformation xmlns:tds="http://www.onvif.org/ver10/device/wsdl" /></s:Body>
        """
    ).encode()


def test_username_token_accepts_second_precision_and_rejects_replay() -> None:
    auth = onvif.OnvifAuth("frigate", "correct horse")
    payload = _request()
    assert auth.valid(payload)
    assert not auth.valid(payload)


def test_username_token_accepts_fractional_timestamp() -> None:
    auth = onvif.OnvifAuth("frigate", "correct horse")
    assert auth.valid(_request(created=time.strftime("%Y-%m-%dT%H:%M:%S.123Z", time.gmtime())))


def test_username_token_rejects_bad_credentials_and_stale_timestamp() -> None:
    auth = onvif.OnvifAuth("frigate", "correct horse")
    assert not auth.valid(_request(username="intruder"))
    stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 301))
    assert not auth.valid(_request(created=stale, nonce=b"fedcba9876543210"))


def test_username_token_rejects_malformed_nonce() -> None:
    auth = onvif.OnvifAuth("frigate", "correct horse")
    assert not auth.valid(_request(nonce=b"short"))


def test_password_file_is_supported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password_file = tmp_path / "onvif-password"
    password_file.write_text("correct horse\n", encoding="utf-8")
    monkeypatch.delenv("OSPREY_ONVIF_PASSWORD", raising=False)
    monkeypatch.setenv("OSPREY_ONVIF_USERNAME", "frigate")
    monkeypatch.setenv("OSPREY_ONVIF_PASSWORD_FILE", os.fspath(password_file))
    auth = onvif.OnvifAuth.from_environment()
    assert auth.enabled
    assert auth.valid(_request())


def test_partial_configuration_does_not_enable_anonymous_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSPREY_ONVIF_USERNAME", "frigate")
    monkeypatch.delenv("OSPREY_ONVIF_PASSWORD", raising=False)
    monkeypatch.delenv("OSPREY_ONVIF_PASSWORD_FILE", raising=False)
    with pytest.raises(RuntimeError):
        onvif.OnvifAuth.from_environment()
    assert not onvif.OnvifAuth("frigate", None).valid(_request())
