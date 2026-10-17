"""RFC 6455 framing and the accepting side of the upgrade — standard library only.

Enough to accept a connection, exchange binary and text frames, and send pings,
without pulling in a WebSocket library.

Masking follows the RFC: frames from a client are masked, frames from a server are
not. `encode_frame` does either, and `FrameReader` reads either, so the same code
serves both ends. `wsclient` has the dialling side of the handshake.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

GUID: Final = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONTROL_PATH: Final = "/camera/1.0/ws"


class Opcode(IntEnum):
    CONTINUATION = 0x0
    TEXT = 0x1
    BINARY = 0x2
    CLOSE = 0x8
    PING = 0x9
    PONG = 0xA


@dataclass(frozen=True)
class Upgrade:
    path: str
    key: str
    subprotocol: str
    headers: dict[str, str]

    @property
    def camera_mac(self) -> str:
        return self.headers.get("camera-mac", "")

    @property
    def camera_model(self) -> str:
        return self.headers.get("camera-model", "")

    @property
    def camera_firmware(self) -> str:
        return self.headers.get("camera-firmware", "")

    @property
    def already_adopted(self) -> bool:
        return self.headers.get("adopted", "").lower() == "true"


class ProtocolError(Exception):
    pass


def parse_upgrade(raw: bytes) -> Upgrade:
    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    if not lines or " " not in lines[0]:
        raise ProtocolError("malformed request line")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        raise ProtocolError("malformed request line")
    path = parts[1]

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()

    if "websocket" not in headers.get("upgrade", "").lower():
        raise ProtocolError("not a websocket upgrade")
    key = headers.get("sec-websocket-key", "")
    if not key:
        raise ProtocolError("missing Sec-WebSocket-Key")
    return Upgrade(
        path=path,
        key=key,
        subprotocol=headers.get("sec-websocket-protocol", ""),
        headers=headers,
    )


def accept_key(key: str) -> str:
    digest = hashlib.sha1(key.encode("ascii") + GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake_response(upgrade: Upgrade) -> bytes:
    """101 with the subprotocol echoed back — the camera expects its own back."""
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_key(upgrade.key)}",
    ]
    if upgrade.subprotocol:
        lines.append(f"Sec-WebSocket-Protocol: {upgrade.subprotocol}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def encode_frame(payload: bytes, opcode: Opcode = Opcode.BINARY, mask: bool = False) -> bytes:
    out = bytearray([0x80 | int(opcode)])
    length = len(payload)
    flag = 0x80 if mask else 0x00
    if length < 126:
        out.append(flag | length)
    elif length < 1 << 16:
        out.append(flag | 126)
        out += struct.pack(">H", length)
    else:
        out.append(flag | 127)
        out += struct.pack(">Q", length)
    if mask:
        key = os.urandom(4)
        out += key
        out += bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    else:
        out += payload
    return bytes(out)


@dataclass
class Frame:
    opcode: Opcode
    payload: bytes


class FrameReader:
    """Accumulates bytes and yields whole frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buffer.extend(chunk)
        frames: list[Frame] = []
        while True:
            frame = self._take()
            if frame is None:
                return frames
            frames.append(frame)

    def _take(self) -> Frame | None:
        buf = self._buffer
        if len(buf) < 2:
            return None
        raw_opcode = buf[0] & 0x0F
        masked = bool(buf[1] & 0x80)
        length = buf[1] & 0x7F
        cursor = 2
        if length == 126:
            if len(buf) < cursor + 2:
                return None
            length = struct.unpack(">H", buf[cursor : cursor + 2])[0]
            cursor += 2
        elif length == 127:
            if len(buf) < cursor + 8:
                return None
            length = struct.unpack(">Q", buf[cursor : cursor + 8])[0]
            cursor += 8
        key = b""
        if masked:
            if len(buf) < cursor + 4:
                return None
            key = bytes(buf[cursor : cursor + 4])
            cursor += 4
        if len(buf) < cursor + length:
            return None
        body = bytes(buf[cursor : cursor + length])
        if masked:
            body = bytes(b ^ key[i % 4] for i, b in enumerate(body))
        del buf[: cursor + length]
        try:
            opcode = Opcode(raw_opcode)
        except ValueError:
            return self._take()
        return Frame(opcode=opcode, payload=body)


def send(sock: socket.socket, payload: bytes, opcode: Opcode = Opcode.BINARY) -> None:
    sock.sendall(encode_frame(payload, opcode))


def ping(sock: socket.socket) -> None:
    """The camera judges a controller that never pings to be dead."""
    send(sock, b"", Opcode.PING)
