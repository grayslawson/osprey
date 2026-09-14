"""Snapshot upload — port 7444.

Snapshots are not pulled. The controller sends `GetRequest` carrying a one-time
HTTPS URL and the camera POSTs the JPEG back to it, so cuckoo has to be the
server: mint a token, hand over the URL, wait for the upload against that token.

Tokens are single use. An unclaimed one expires rather than accumulating.
"""

from __future__ import annotations

import logging
import secrets
import ssl
import threading
import time
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

SNAPSHOT_PORT: Final = 7444
UPLOAD_PREFIX: Final = "/internal/camera-upload/"
TOKEN_TTL_SEC: Final = 120.0
MAX_UPLOAD_BYTES: Final = 16 * 1024 * 1024

log = logging.getLogger("cuckoo.snapshot")


class Pending:
    """One awaited upload."""

    def __init__(self, mac: str) -> None:
        self.mac = mac
        self.created_at = time.time()
        self.image: bytes | None = None
        self._arrived = threading.Event()

    def deliver(self, image: bytes) -> None:
        self.image = image
        self._arrived.set()

    def wait(self, timeout: float) -> bytes | None:
        return self.image if self._arrived.wait(timeout) else None

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > TOKEN_TTL_SEC


class Store:
    """Outstanding tokens, plus the last image seen for each camera."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._pending: dict[str, Pending] = {}
        self.latest: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def mint(self, mac: str) -> tuple[str, str]:
        """A token and the URL to hand the camera."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._drop_expired()
            self._pending[token] = Pending(mac)
        return token, f"{self.base_url}{UPLOAD_PREFIX}{token}"

    def claim(self, token: str, image: bytes) -> bool:
        """Accept an upload. False if the token is unknown or already used."""
        with self._lock:
            pending = self._pending.pop(token, None)
        if pending is None or pending.expired:
            return False
        pending.deliver(image)
        with self._lock:
            self.latest[pending.mac] = image
        return True

    def awaiting(self, token: str) -> Pending | None:
        with self._lock:
            return self._pending.get(token)

    def wait(self, token: str, timeout: float = 10.0) -> bytes | None:
        pending = self.awaiting(token)
        if pending is None:
            return None
        return pending.wait(timeout)

    @property
    def outstanding(self) -> int:
        with self._lock:
            self._drop_expired()
            return len(self._pending)

    def _drop_expired(self) -> None:
        for token in [t for t, p in self._pending.items() if p.expired]:
            del self._pending[token]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def store(self) -> Store:
        server = self.server
        assert isinstance(server, SnapshotServer)
        return server.store

    def log_message(self, format: str, *args: object) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def do_POST(self) -> None:  # noqa: N802 - name fixed by http.server
        if not self.path.startswith(UPLOAD_PREFIX):
            self._respond(HTTPStatus.NOT_FOUND)
            return
        token = self.path[len(UPLOAD_PREFIX) :].split("?", 1)[0]
        body = self._read_body()
        if body is None:
            self._respond(HTTPStatus.BAD_REQUEST)
            return
        body = _extract_upload(body, self.headers.get("Content-Type", ""))
        if body is None:
            self._respond(HTTPStatus.BAD_REQUEST)
            return
        if not self.store.claim(token, body):
            log.warning("upload for unknown or expired token")
            self._respond(HTTPStatus.NOT_FOUND)
            return
        log.info("snapshot received, %d bytes", len(body))
        self._respond(HTTPStatus.OK)

    do_PUT = do_POST

    def _read_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._read_chunked()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            return None
        body = self.rfile.read(length)
        return body if len(body) == length else None

    def _read_chunked(self) -> bytes | None:
        body = bytearray()
        while True:
            line = self.rfile.readline(65)
            if not line.endswith(b"\r\n") or len(line) > 64:
                return None
            try:
                size_text = line[:-2].split(b";", 1)[0]
                if not size_text:
                    return None
                size = int(size_text, 16)
            except ValueError:
                return None
            if size == 0:
                while True:
                    trailer = self.rfile.readline(65)
                    if not trailer.endswith(b"\r\n") or len(trailer) > 64:
                        return None
                    if trailer == b"\r\n":
                        return bytes(body)
                    if b":" not in trailer[:-2]:
                        return None
            if len(body) + size > MAX_UPLOAD_BYTES:
                return None
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                return None
            body += chunk

    def _respond(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _extract_upload(body: bytes, content_type: str) -> bytes | None:
    """Return the JPEG from a raw or camera multipart upload, bounded already."""
    if content_type.lower().split(";", 1)[0].strip() != "multipart/form-data":
        return body if body.startswith(b"\xff\xd8") else None
    match = re.search(r"(?:^|;)\s*boundary=(?:\"([^\"]+)\"|([^;\s]+))", content_type, re.I)
    if not match:
        return None
    try:
        boundary = (match.group(1) or match.group(2)).encode("ascii", "strict")
    except UnicodeEncodeError:
        return None
    marker = b"--" + boundary
    opening = marker + b"\r\n"
    if not body.startswith(opening):
        return None
    header_end = body.find(b"\r\n\r\n", len(opening))
    if header_end < 0:
        return None
    header_block = body[len(opening) : header_end]
    headers = header_block.lower().split(b"\r\n")
    if not any(line.startswith(b"content-disposition:") for line in headers):
        return None
    payload_start = header_end + 4
    payload_end = body.find(b"\r\n" + marker, payload_start)
    if payload_end < 0:
        return None
    image = body[payload_start:payload_end]
    suffix = body[payload_end + 2 + len(marker) :]
    if suffix not in (b"--", b"--\r\n"):
        return None
    return image if image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9") else None


class SnapshotServer(ThreadingHTTPServer):
    """HTTPS listener the camera uploads to."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store: Store, cert: Path | None, port: int = SNAPSHOT_PORT) -> None:
        self.store = store
        super().__init__(("0.0.0.0", port), _Handler, bind_and_activate=False)
        if cert is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert))
            self.socket = context.wrap_socket(self.socket, server_side=True)
        self.server_bind()
        self.server_activate()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        log.info("snapshot uploads accepted on :%d", self.server_address[1])
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
