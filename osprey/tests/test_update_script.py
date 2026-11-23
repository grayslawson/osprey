"""osprey-update.sh --latest resolves a pre-release when /releases/latest is 404."""

from __future__ import annotations

import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "osprey-update.sh"


def _resolve(routes: dict[str, str]) -> subprocess.CompletedProcess[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = routes.get(self.path)
            self.send_response(200 if body is not None else 404)
            data = (body or "{}").encode()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Run only the resolver: the functions sit between these two markers.
    text = SCRIPT.read_text()
    start = text.index("fetch_release_json() {")
    end = text.index('if [ "$latest" = true ]')
    program = 'die() { echo "$*" >&2; exit 1; }\n' + text[start:end] + "\nlatest_release_tag\n"
    try:
        return subprocess.run(
            ["sh", "-c", program], capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                 "OSPREY_RELEASE_API_URL": f"http://127.0.0.1:{server.server_port}/releases/latest"},
        )
    finally:
        server.shutdown()


def test_latest_prefers_the_stable_release() -> None:
    result = _resolve({"/releases/latest": '{"tag_name": "v1.0.0"}'})
    assert result.stdout.strip() == "v1.0.0"


def test_latest_falls_back_to_a_prerelease_when_latest_is_404() -> None:
    result = _resolve({"/releases?per_page=1": '[{"tag_name": "v0.9.0b3", "prerelease": true}]'})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "v0.9.0b3"


def test_no_releases_at_all_is_an_error() -> None:
    result = _resolve({})
    assert result.returncode != 0 and "could not query" in result.stderr
