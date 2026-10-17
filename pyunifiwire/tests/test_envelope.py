"""The control-channel envelope.

These moved here from the two implementations that used to own them: they test the
wire itself, not either end of it.
"""

from __future__ import annotations

import json

import pytest

from unifiwire import envelope
from unifiwire.envelope import (
    CAMERA,
    CONTROLLER,
    DecodeError,
    Envelope,
    Ids,
    decode,
    reply_to,
    request,
)

# ----------------------------------------------------------------------- envelope


def test_envelope_round_trips() -> None:
    ids = envelope.Ids()
    sent = envelope.request("ChangeOsdSettings", {"enableOverlay": 1}, ids)
    back = envelope.decode(sent.to_json())
    assert back.function_name == "ChangeOsdSettings"
    assert back.payload == {"enableOverlay": 1}
    assert back.sender == envelope.CONTROLLER
    assert back.recipient == envelope.CAMERA


def test_reply_echoes_name_and_carries_source_id() -> None:
    ids = envelope.Ids()
    incoming = envelope.Envelope(
        function_name=envelope.HELLO, payload={}, message_id=81068042, sender=envelope.CAMERA
    )
    out = envelope.reply_to(incoming, {"protocolVersion": 67}, ids)
    assert out.function_name == envelope.HELLO
    assert out.in_response_to == 81068042
    assert out.is_reply


def test_ids_are_monotonic() -> None:
    ids = envelope.Ids(start=10)
    assert [ids.next(), ids.next(), ids.next()] == [10, 11, 12]


def test_decode_tolerates_leading_bytes() -> None:
    raw = b"\x00\x02" + json.dumps({"functionName": "X", "messageId": 1}).encode()
    assert envelope.decode(raw).function_name == "X"


@pytest.mark.parametrize("bad", [b"", b"not json", b"[]", b'{"no":"name"}'])
def test_decode_rejects_rubbish(bad: bytes) -> None:
    with pytest.raises(envelope.DecodeError):
        envelope.decode(bad)
