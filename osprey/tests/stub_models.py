"""A local HTTP stub that speaks the OpenAI chat and decisions dialects, with scripted misbehaviour."""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Callable
from typing import Any

# A scripted reply is a dict (served as JSON, 200), or one of these behaviours.
INVALID_JSON = "invalid_json"   # chat only: a 200 whose message content is not JSON
MALFORMED = "malformed"         # a 200 whose body is not JSON
SLOW = "slow"                   # sleep past any sane client timeout, then answer
HTTP_429 = "http_429"
HTTP_500 = "http_500"

Reply = dict[str, Any] | str | Callable[[dict[str, Any]], Any]


def chat_body(content: dict[str, Any] | str, *, cost: float | None = 0.0002) -> dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content)
    usage: dict[str, Any] = {"prompt_tokens": 120, "completion_tokens": 30}
    if cost is not None:
        usage["cost"] = cost
    return {"model": "stub-chat", "choices": [{"message": {"role": "assistant", "content": text}}], "usage": usage}


def decisions_body(answers: dict[str, Any], *, cost: float = 0.0001) -> dict[str, Any]:
    return {"model": "stub-decisions", "usage": {"cost": cost}, "answers": answers}


class StubModels:
    def __init__(self, chat_reply: Reply | list[Reply] | None = None,
                 decisions_reply: Reply | list[Reply] | None = None, slow_sec: float = 3.0) -> None:
        self.script = {"/chat/completions": self._listify(chat_reply), "/decisions": self._listify(decisions_reply)}
        self.requests: list[dict[str, Any]] = []
        self.slow_sec = slow_sec
        self._lock = threading.Lock()
        self._srv: http.server.ThreadingHTTPServer | None = None
        self.url = ""

    @staticmethod
    def _listify(r: Reply | list[Reply] | None) -> list[Reply]:
        return r if isinstance(r, list) else [r] if r is not None else []

    def _next(self, path: str) -> Reply | None:
        with self._lock:
            q = self.script.get(path, [])
            # the last scripted reply repeats
            return (q.pop(0) if len(q) > 1 else q[0]) if q else None

    def __enter__(self) -> StubModels:
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw)
                with stub._lock:
                    stub.requests.append({"method": "POST", "path": self.path,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}, "json": body})
                r = stub._next(self.path)
                if callable(r):
                    r = r(body)
                if r is None:
                    return self._send(404, b"{}")
                if r == SLOW:
                    time.sleep(stub.slow_sec)
                    return self._send(200, b"{}")
                if r == HTTP_429:
                    return self._send(429, b'{"error":"rate"}')
                if r == HTTP_500:
                    return self._send(500, b'{"error":"boom"}')
                if r == MALFORMED:
                    return self._send(200, b"<html>not json")
                if r == INVALID_JSON:
                    r = chat_body("I think it is glare, sorry")
                self._send(200, json.dumps(r).encode())

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, args=(0.02,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        return self

    def __exit__(self, *exc: object) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
