"""WebSocket framing and the handshake, both directions.

These moved here from the two implementations that used to own them: they test the
wire itself, not either end of it.
"""

from __future__ import annotations

import pytest

from unifiwire import ws

# ----------------------------------------------------------------------------- ws


UPGRADE = (
    b"GET /camera/1.0/ws?token=abc HTTP/1.1\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    b"Sec-WebSocket-Protocol: secure_transfer\r\n"
    b"camera-mac: AABBCCDDEEFF\r\n"
    b"camera-model: UVC G5 PTZ\r\n"
    b"adopted: false\r\n\r\n"
)


def test_parse_upgrade_reads_camera_headers() -> None:
    up = ws.parse_upgrade(UPGRADE)
    assert up.path.startswith(ws.CONTROL_PATH)
    assert up.subprotocol == "secure_transfer"
    assert up.camera_mac == "AABBCCDDEEFF"
    assert up.camera_model == "UVC G5 PTZ"
    assert not up.already_adopted


def test_accept_key_matches_rfc_example() -> None:
    assert ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_handshake_echoes_subprotocol() -> None:
    response = ws.handshake_response(ws.parse_upgrade(UPGRADE))
    assert b"101 Switching Protocols" in response
    assert b"Sec-WebSocket-Protocol: secure_transfer" in response


def test_the_subprotocol_is_reported_verbatim() -> None:
    """Two sockets arrive on one path; the subprotocol is what tells them apart."""
    raw = UPGRADE.replace(b"secure_transfer", b"ptz1")
    assert ws.parse_upgrade(raw).subprotocol == "ptz1"


@pytest.mark.parametrize("bad", [b"GET / HTTP/1.1\r\n\r\n", b"nonsense"])
def test_parse_upgrade_rejects_non_upgrade(bad: bytes) -> None:
    with pytest.raises(ws.ProtocolError):
        ws.parse_upgrade(bad)


def test_frames_round_trip_masked_and_unmasked() -> None:
    reader = ws.FrameReader()
    payload = b'{"functionName":"X"}'
    # The camera masks; we do not.
    frames = reader.feed(ws.encode_frame(payload, ws.Opcode.BINARY, mask=True))
    assert [f.payload for f in frames] == [payload]
    frames = reader.feed(ws.encode_frame(payload, ws.Opcode.TEXT, mask=False))
    assert frames[0].opcode is ws.Opcode.TEXT


def test_frame_reader_handles_split_and_batched() -> None:
    reader = ws.FrameReader()
    blob = ws.encode_frame(b"one", mask=True) + ws.encode_frame(b"two", mask=True)
    assert reader.feed(blob[:3]) == []
    frames = reader.feed(blob[3:])
    assert [f.payload for f in frames] == [b"one", b"two"]


@pytest.mark.parametrize("size", [0, 125, 126, 300, 70_000])
def test_frame_length_encodings(size: int) -> None:
    reader = ws.FrameReader()
    payload = b"x" * size
    frames = reader.feed(ws.encode_frame(payload, mask=True))
    assert frames[0].payload == payload
