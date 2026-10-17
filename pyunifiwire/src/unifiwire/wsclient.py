"""The dialling side of the WebSocket: the handshake a camera opens with.

`ws` accepts connections; this makes them. The framing is shared — a client's
frames must be masked, which `ws.encode_frame` already does — so what is left is
the opening request, header for header as a real camera sends it, and checking the
server's answer.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import ws

DEFAULT_PORT: Final = 7442
SUBPROTOCOL: Final = "secure_transfer"
HANDSHAKE_TIMEOUT_SEC: Final = 15.0


class HandshakeError(Exception):
    pass


def client_key() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def upgrade_request(
    host: str,
    port: int,
    path: str,
    key: str,
    mac: str,
    subprotocol: str = SUBPROTOCOL,
    token: str = "",
    model: str = "",
    firmware: str = "",
    adopted: bool = False,
    camera_ip: str = "",
    device_id: str = "",
    guid: str = "",
) -> bytes:
    """The camera's opening request, header for header as the real one sends it.

    Measured off the real G5 rather than guessed, because the controller proxies
    this handshake upstream and the upstream is strict about it:

    * `Host` carries no port
    * `Connection: close, Upgrade`
    * `camera-model` is the **hex system id** (`0xa59b`), not the model name
    * `device-id`, `x-guid` and `origin` are present and are not decorative

    The token rides in the query string, and only until the camera is adopted.
    """
    target = f"{path}?token={token}" if token else path
    lines = [
        f"GET {target} HTTP/1.1",
        f"Host: {host}",
        "Pragma: no-cache",
        "Cache-Control: no-cache",
        "Upgrade: websocket",
        "Connection: close, Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Protocol: {subprotocol}",
        f"Origin: http://ws_camera_proto_{subprotocol}",
        f"camera-mac: {mac}",
    ]
    if camera_ip:
        lines.append(f"camera-ip: {camera_ip}")
    if model:
        lines.append(f"camera-model: {model}")
    if firmware:
        lines.append(f"camera-firmware: {firmware}")
    if device_id:
        lines.append(f"device-id: {device_id}")
    if guid:
        lines.append(f"x-guid: {guid}")
    lines.append(f"adopted: {'true' if adopted else 'false'}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


@dataclass(frozen=True)
class Accepted:
    """A completed handshake: the status line, headers, and any early bytes."""

    status: int
    headers: dict[str, str]
    leftover: bytes

    @property
    def subprotocol(self) -> str:
        return self.headers.get("sec-websocket-protocol", "")


def read_response(raw: bytes, key: str) -> Accepted:
    """Parse the server's answer and check it answers the key that was sent."""
    head, separator, leftover = raw.partition(b"\r\n\r\n")
    if not separator:
        raise HandshakeError("incomplete handshake response")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2 or not parts[1].isdigit():
        raise HandshakeError(f"malformed status line: {lines[0]!r}")
    status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if status != 101:
        raise HandshakeError(f"refused with {status}: {headers.get('x-reason', lines[0])}")
    expected = ws.accept_key(key)
    if headers.get("sec-websocket-accept") != expected:
        raise HandshakeError("Sec-WebSocket-Accept does not match the key sent")
    return Accepted(status=status, headers=headers, leftover=leftover)


def tls_context(certificate: Path | None) -> ssl.SSLContext:
    """The controller's certificate is self-signed, so it is not verified.

    Ours is presented as a client certificate: the controller asks for one.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if certificate is not None:
        context.load_cert_chain(str(certificate))
    return context


class Connection:
    """One WebSocket to the controller."""

    def __init__(self, sock: socket.socket, leftover: bytes = b"") -> None:
        self.sock = sock
        self.reader = ws.FrameReader()
        self._pending: list[ws.Frame] = list(self.reader.feed(leftover))

    def send(self, payload: bytes, opcode: ws.Opcode = ws.Opcode.BINARY) -> None:
        self.sock.sendall(ws.encode_frame(payload, opcode, mask=True))

    def pong(self, payload: bytes = b"") -> None:
        self.send(payload, ws.Opcode.PONG)

    def receive(self, timeout: float | None = None) -> list[ws.Frame]:
        """Frames that have arrived. An empty list means nothing yet, not closed."""
        if self._pending:
            frames, self._pending = self._pending, []
            return frames
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
        except (TimeoutError, ssl.SSLWantReadError):
            return []
        except OSError as exc:
            raise ConnectionError(str(exc)) from exc
        if not chunk:
            raise ConnectionError("controller closed the connection")
        return self.reader.feed(chunk)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:  # pragma: no cover - already gone
            pass


def connect(
    host: str,
    port: int,
    mac: str,
    certificate: Path | None,
    token: str = "",
    path: str = ws.CONTROL_PATH,
    subprotocol: str = SUBPROTOCOL,
    model: str = "",
    firmware: str = "",
    adopted: bool = False,
    camera_ip: str = "",
    device_id: str = "",
    guid: str = "",
    timeout: float = HANDSHAKE_TIMEOUT_SEC,
) -> Connection:
    """Dial the controller and complete the upgrade."""
    key = client_key()
    raw = socket.create_connection((host, port), timeout=timeout)
    sock = tls_context(certificate).wrap_socket(raw)
    try:
        sock.sendall(
            upgrade_request(
                host, port, path, key, mac, subprotocol, token, model, firmware, adopted,
                camera_ip, device_id, guid,
            )
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise HandshakeError("controller closed during handshake")
            response += chunk
        accepted = read_response(response, key)
    except Exception:
        sock.close()
        raise
    sock.settimeout(None)
    return Connection(sock, accepted.leftover)
