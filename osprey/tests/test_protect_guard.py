"""``osprey protect-guard`` against a fake UniFi Network integration API."""

from __future__ import annotations

import http.server
import json
import ssl
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from unifiwire import certs

import protect_guard

CAMERA = "192.0.2.20"
ZONES = [{"id": "z-gw", "name": "Gateway"}, {"id": "z-in", "name": "Internal"}, {"id": "z-ex", "name": "External"}]


class FakeApi:
    def __init__(self, policies: list[dict[str, Any]], zones: list[dict[str, Any]] = ZONES) -> None:
        self.policies = policies
        self.zones = zones
        self.created: list[dict[str, Any]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        route, _, query = path.partition("?")
        offset = int(dict(p.split("=") for p in query.split("&")).get("offset", 0)) if query else 0
        if method == "POST" and route == "/sites/s1/firewall/policies":
            assert body is not None
            self.created.append(body)
            return {"id": "new-policy", **body}
        table = {
            "/sites": [{"id": "s1", "internalReference": "default", "name": "Default"}],
            "/sites/s1/firewall/zones": self.zones,
            "/sites/s1/firewall/policies": self.policies,
        }[route]
        page = table[offset:offset + 2]  # small pages prove pagination
        return {"data": page, "totalCount": len(table)}


def test_missing_guard_is_reported_then_created() -> None:
    api = FakeApi([{"enabled": True, "name": "unrelated", "action": {"type": "ALLOW"}}] * 3)
    assert protect_guard.run(api, CAMERA, "default", apply=False)[0] == 1
    assert api.created == []
    code, message = protect_guard.run(api, CAMERA, "default", apply=True)
    assert code == 0 and "new-policy" in message
    policy = api.created[0]
    assert policy["action"]["type"] == "BLOCK"
    assert policy["source"]["zoneId"] == "z-in" and policy["destination"]["zoneId"] == "z-gw"
    # What it creates is what it later recognises.
    assert protect_guard.find_guard([policy], CAMERA, "z-gw") is policy


def test_existing_hand_made_guard_is_accepted() -> None:
    hand_made = protect_guard.guard_policy(CAMERA, "z-in", "z-gw")
    hand_made["destination"]["trafficFilter"]["portFilter"]["items"] = [
        {"type": "PORT_NUMBER_RANGE", "start": 7440, "stop": 7560}]
    api = FakeApi([hand_made])
    assert protect_guard.run(api, CAMERA, "default", apply=True)[0] == 0
    assert api.created == []


def test_disabled_allow_or_wrong_camera_does_not_count() -> None:
    base = protect_guard.guard_policy(CAMERA, "z-in", "z-gw")
    disabled = {**base, "enabled": False}
    allow = {**base, "action": {"type": "ALLOW"}}
    other = protect_guard.guard_policy("192.0.2.99", "z-in", "z-gw")
    assert protect_guard.find_guard([disabled, allow, other], CAMERA, "z-gw") is None


def test_console_without_zone_firewall_cannot_tell() -> None:
    api = FakeApi([], zones=[])
    assert protect_guard.run(api, CAMERA, "default", apply=True)[0] == 2
    assert api.created == []


# --- TLS verification -------------------------------------------------------


@pytest.fixture
def https_console(tmp_path: Path):  # type: ignore[no-untyped-def]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    def serve(name: str) -> tuple[http.server.HTTPServer, str]:
        pem = certs.ensure(tmp_path / f"{name}.pem")
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(pem)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"https://127.0.0.1:{server.server_port}"

    servers: list[http.server.HTTPServer] = []

    def make(name: str = "a") -> str:
        server, url = serve(name)
        servers.append(server)
        return url

    yield make
    for server in servers:
        server.shutdown()


def test_tls_is_verified_by_default(https_console: Any) -> None:
    url = https_console()
    with pytest.raises(urllib.error.URLError):
        protect_guard.http_client(url, "key")("GET", "/x", None)
    assert protect_guard.http_client(url, "key", insecure=True)("GET", "/x", None) == {"ok": True}


def test_pin_file_trusts_first_certificate_then_refuses_a_different_one(
    https_console: Any, tmp_path: Path
) -> None:
    pin = tmp_path / "pin"
    first = https_console("a")
    assert protect_guard.http_client(first, "key", pin_file=pin)("GET", "/x", None) == {"ok": True}
    assert oct(pin.stat().st_mode & 0o777) == "0o600"
    assert protect_guard.http_client(first, "key", pin_file=pin)("GET", "/x", None) == {"ok": True}
    with pytest.raises(urllib.error.URLError, match="changed"):
        protect_guard.http_client(https_console("b"), "key", pin_file=pin)("GET", "/x", None)

def test_created_policy_blocks_only_the_camera_on_the_adoption_ports() -> None:
    policy = protect_guard.guard_policy(CAMERA, "z-in", "z-gw")
    ports = {i["value"] for i in policy["destination"]["trafficFilter"]["portFilter"]["items"]}
    assert ports == set(protect_guard.GUARD_PORTS) >= protect_guard.REQUIRED_PORTS
    assert policy["source"]["trafficFilter"]["ipAddressFilter"]["items"] == [{"type": "IP_ADDRESS", "value": CAMERA}]
    assert policy["enabled"] is True and policy["action"] == {"type": "BLOCK"}


def test_inverted_or_partial_matches_do_not_count_as_a_guard() -> None:
    base = protect_guard.guard_policy(CAMERA, "z-in", "z-gw")
    inverted_ports = protect_guard.guard_policy(CAMERA, "z-in", "z-gw")
    inverted_ports["destination"]["trafficFilter"]["portFilter"]["matchOpposite"] = True
    inverted_source = protect_guard.guard_policy(CAMERA, "z-in", "z-gw")
    inverted_source["source"]["trafficFilter"]["ipAddressFilter"]["matchOpposite"] = True
    partial = protect_guard.guard_policy(CAMERA, "z-in", "z-gw")
    partial["destination"]["trafficFilter"]["portFilter"]["items"] = [{"type": "PORT_NUMBER", "value": 7442}]
    wrong_zone = {**base, "destination": {**base["destination"], "zoneId": "z-ex"}}
    assert protect_guard.find_guard([inverted_ports, inverted_source, partial, wrong_zone], CAMERA, "z-gw") is None
    assert protect_guard.find_guard([{**base, "action": {"type": "REJECT"}}], CAMERA, "z-gw") is not None


def test_unknown_site_cannot_tell() -> None:
    code, message = protect_guard.run(FakeApi([]), CAMERA, "nope", apply=True)
    assert code == 2 and "nope" in message


class _UniFi(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str, bytes, str | None]]] = []

    def _reply(self, code: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.seen.append(("GET", self.path, b"", self.headers.get("X-API-KEY")))
        base = "/proxy/network/integration/v1"
        table = {
            base + "/sites": [{"id": "s1", "internalReference": "default"}],
            base + "/sites/s1/firewall/zones": ZONES,
            base + "/sites/s1/firewall/policies": [],
        }
        path = self.path.partition("?")[0]
        if path in table:
            self._reply(200, {"data": table[path], "totalCount": len(table[path])})
        else:
            self._reply(404, {})

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.seen.append(("POST", self.path, body, self.headers.get("X-API-KEY")))
        self._reply(200, {"id": "created-1"})

    def log_message(self, *args: object) -> None:
        pass


def _console(handler: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_main_checks_then_applies_over_real_http(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = tmp_path / "key"
    key.write_text("secret-key\n")
    _UniFi.seen.clear()
    server, console = _console(_UniFi)
    try:
        argv = ["--console", console, "--camera-ip", CAMERA, "--api-key-file", str(key)]
        assert protect_guard.main(argv) == 1  # nothing created without --apply
        assert not [r for r in _UniFi.seen if r[0] == "POST"]
        assert protect_guard.main([*argv, "--apply"]) == 0
    finally:
        server.shutdown()
    posts = [r for r in _UniFi.seen if r[0] == "POST"]
    assert len(posts) == 1 and posts[0][1].endswith("/sites/s1/firewall/policies")
    assert json.loads(posts[0][2])["source"]["trafficFilter"]["ipAddressFilter"]["items"][0]["value"] == CAMERA
    assert all(r[3] == "secret-key" for r in _UniFi.seen)
    assert "created policy created-1" in capsys.readouterr().out


def test_main_reports_http_errors_and_unreachable_console(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "key"
    key.write_text("k")
    server, console = _console(BaseHTTPRequestHandler)  # answers 501 to GET
    try:
        assert protect_guard.main(["--console", console, "--camera-ip", CAMERA, "--api-key-file", str(key)]) == 2
        assert "HTTP 501" in capsys.readouterr().out
    finally:
        server.shutdown()
    assert protect_guard.main(["--console", "http://127.0.0.1:9", "--camera-ip", CAMERA, "--api-key-file", str(key)]) == 2
    assert "cannot reach" in capsys.readouterr().out


def test_api_key_must_be_supplied_and_non_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OSPREY_UNIFI_API_KEY_FILE", raising=False)
    with pytest.raises(SystemExit, match="--api-key-file"):
        protect_guard._api_key(None)
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(SystemExit, match="empty"):
        protect_guard._api_key(str(empty))
    monkeypatch.setenv("OSPREY_UNIFI_API_KEY_FILE", str(empty))
    with pytest.raises(SystemExit, match="empty"):
        protect_guard._api_key(None)
