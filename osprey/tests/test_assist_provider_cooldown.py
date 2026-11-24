"""A capped or rate-limited model is not called again on every check (per-model cooldown)."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from assist.providers import ProviderError, build_provider, parse_provider


class _Gateway:
    """Answers every POST with the queued (status, body); counts calls per model."""

    def __init__(self) -> None:
        self.replies: list[tuple[int, dict[str, Any]]] = []
        self.calls: list[str] = []
        gw = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                gw.calls.append(body.get("model", ""))
                code, payload = gw.replies.pop(0) if gw.replies else (200, {"answers": {}})
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def gateway() -> Iterator[_Gateway]:
    gw = _Gateway()
    yield gw
    gw.server.shutdown()


def _provider(gw: _Gateway, now: list[float]) -> Any:
    cfg = parse_provider("gw", {"kind": "decisions", "base_url": gw.url, "path": "/decisions"})
    return build_provider("gw", cfg, clock=lambda: now[0])


def _ask(p: Any, model: str) -> None:
    p.decide(model, ["state"], {"q": {"type": "noul", "instructions": "?"}})


def test_daily_cap_skips_the_model_until_the_next_utc_day(gateway: _Gateway) -> None:
    now = [1_800_000_000.0 + 3600]  # an hour into a UTC day
    p = _provider(gateway, now)
    gateway.replies = [(429, {"error": {"message": "litellm.RateLimitError: No deployments available for selected model"}})]
    with pytest.raises(ProviderError):
        _ask(p, "capped")
    with pytest.raises(ProviderError, match="cooling down"):
        _ask(p, "capped")
    _ask(p, "other")  # another model on the same provider is unaffected
    assert gateway.calls == ["capped", "other"]
    now[0] += 86400 - 3600  # next UTC midnight
    _ask(p, "capped")
    assert gateway.calls == ["capped", "other", "capped"]


def test_rate_limit_backs_off_and_doubles_then_resets_on_success(gateway: _Gateway) -> None:
    now = [1_800_000_000.0]
    p = _provider(gateway, now)
    gateway.replies = [(429, {"error": {"message": "Provider returned error: rate limited"}}),
                       (502, {"error": {"message": "bad gateway"}})]
    with pytest.raises(ProviderError):
        _ask(p, "m")
    with pytest.raises(ProviderError, match="cooling down"):
        _ask(p, "m")
    now[0] += 61
    with pytest.raises(ProviderError, match="http 502"):
        _ask(p, "m")
    now[0] += 61  # backoff doubled to 120 s
    with pytest.raises(ProviderError, match="cooling down"):
        _ask(p, "m")
    now[0] += 60
    _ask(p, "m")  # success clears the cooldown
    _ask(p, "m")
    assert gateway.calls == ["m", "m", "m", "m"]
