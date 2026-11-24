"""A stand-in Frigate config API for running the setup wizard end to end.

Serves the calls Osprey makes: ``GET /api/version``, ``GET /api/config/raw``
(the YAML as a JSON string, as Frigate sends it) and ``POST /api/config/save``.
With ``--user``/``--password`` it requires ``POST /api/login`` first, like
Frigate's authenticated port. Saved configs are printed, not applied::

    python3 fakefrigate.py --port 5000 [--user admin --password secret]

A development tool, like stubcam.py; not shipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = """mqtt:
  enabled: false

cameras:
  garden:
    ffmpeg:
      inputs:
        - path: rtsp://192.0.2.50:554/stream
          roles: [record]
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--user", default="")
    parser.add_argument("--password", default="")
    args = parser.parse_args(argv)
    state = {"config": CONFIG}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def send(self, code: int, body: object, headers: dict[str, str] | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def allowed(self) -> bool:
            if not args.user or "frigate_token=fake" in self.headers.get("Cookie", ""):
                return True
            self.send(401, {"message": "Unauthorized"})
            return False

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/version" and self.allowed():
                self.send(200, "0.16.0-fake")
            elif self.path == "/api/config/raw" and self.allowed():
                self.send(200, state["config"])
            elif self.path.startswith("/api/"):
                self.send(404, {"message": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/api/login":
                ok = json.loads(body or b"{}") == {"user": args.user, "password": args.password}
                print(f"fakefrigate: login {'accepted' if ok else 'refused'}", file=sys.stderr)
                if ok:
                    self.send(200, "", {"Set-Cookie": "frigate_token=fake; Path=/; HttpOnly"})
                else:
                    self.send(401, {"message": "Login failed"})
            elif self.path.startswith("/api/config/save") and self.allowed():
                state["config"] = body.decode()
                print(f"fakefrigate: saved ({self.path}):\n{state['config']}", file=sys.stderr)
                self.send(200, {"success": True, "message": "Config successfully saved, restarting (this can take up to one minute)..."})

    print(f"fakefrigate: listening on :{args.port}", file=sys.stderr)
    ThreadingHTTPServer(("", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
