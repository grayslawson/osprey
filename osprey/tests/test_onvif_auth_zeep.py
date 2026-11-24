"""The real zeep UsernameToken (what Frigate's ONVIF client sends) against OnvifAuth with auth on.

Guards the bug fixed in #84: zeep writes Created as ``2026-11-24T20:12:44+00:00``, and Osprey once rejected every Frigate login
as a malformed timestamp. zeep is a test-only dependency (requirements-ci.txt); there is no skip, so a missing install fails.
"""

from __future__ import annotations

import re

import onvif
from lxml import etree  # type: ignore[import-untyped]
from zeep.wsse.username import UsernameToken


def _zeep_request(password: str) -> bytes:
    env = etree.fromstring(onvif.envelope("<s:Header/>").encode())
    token = UsernameToken("frigate", password, use_digest=True)  # type: ignore[no-untyped-call]
    token.apply(env, {})  # type: ignore[no-untyped-call]
    return bytes(etree.tostring(env))


def test_zeep_token_with_the_right_password_is_accepted() -> None:
    payload = _zeep_request("correct horse")
    created = re.search(rb"Created[^>]*>([^<]+)<", payload)
    assert created is not None and created.group(1).endswith(b"+00:00"), "zeep no longer sends the +00:00 form; update this test"
    assert onvif.OnvifAuth("frigate", "correct horse").valid(payload)


def test_zeep_token_with_the_wrong_password_is_rejected() -> None:
    assert not onvif.OnvifAuth("frigate", "correct horse").valid(_zeep_request("wrong horse"))
