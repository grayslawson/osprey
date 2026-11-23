"""The container health probe treats first-run "waiting" as healthy."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

spec = importlib.util.spec_from_file_location(
    "osprey_health", Path(__file__).resolve().parents[2] / "docker" / "osprey_health.py"
)
assert spec and spec.loader
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def _serve(code: int, body: dict[str, Any]) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("code, body, expected", [
    (200, {"status": "ok"}, 0),
    (503, {"status": "waiting", "camera": False}, 0),
    (503, {"status": "degraded"}, 1),
    (500, {"status": "waiting"}, 1),
])
def test_health_exit_codes(monkeypatch: pytest.MonkeyPatch, code: int, body: dict[str, Any], expected: int) -> None:
    server = _serve(code, body)
    monkeypatch.setenv("OSPREY_HEALTH_HOST", "127.0.0.1")
    monkeypatch.setenv("OSPREY_HEALTH_PORT", str(server.server_port))
    try:
        assert health.main() == expected
    finally:
        server.shutdown()
