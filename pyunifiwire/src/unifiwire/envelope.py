"""Control-channel envelope: the JSON wrapper every management message rides in."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

CAMERA: Final = "ubnt_avclient"
CONTROLLER: Final = "UniFiVideo"

HELLO: Final = "ubnt_avclient_hello"
PARAM_AGREEMENT: Final = "ubnt_avclient_paramAgreement"
TIME_SYNC: Final = "ubnt_avclient_timeSync"


def timestamp(now: float | None = None) -> str:
    """ISO-8601 with milliseconds and a numeric offset, as the channel expects."""
    moment = datetime.fromtimestamp(now, timezone.utc) if now else datetime.now(timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "+00:00")


@dataclass(frozen=True)
class Envelope:
    function_name: str
    payload: dict[str, Any]
    message_id: int
    in_response_to: int = 0
    sender: str = CONTROLLER
    recipient: str = CAMERA
    response_expected: bool = False
    at: str = ""

    @property
    def is_reply(self) -> bool:
        return self.in_response_to != 0

    @property
    def from_camera(self) -> bool:
        return self.sender == CAMERA

    def to_json(self) -> bytes:
        body: dict[str, Any] = {
            "from": self.sender,
            "to": self.recipient,
            "functionName": self.function_name,
            "messageId": self.message_id,
            "inResponseTo": self.in_response_to,
            "payload": self.payload,
            "responseExpected": self.response_expected,
            "timeStamp": self.at or timestamp(),
        }
        return json.dumps(body, separators=(",", ":")).encode("utf-8")


class DecodeError(ValueError):
    pass


def decode(raw: bytes) -> Envelope:
    """Parse a frame.

    Frames are UTF-8 JSON. Be tolerant of leading bytes before the object: scan
    to the first '{' rather than assuming the payload starts at offset zero.
    """
    text = raw.decode("utf-8", errors="replace")
    start = text.find("{")
    if start < 0:
        raise DecodeError("no JSON object in frame")
    try:
        body = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise DecodeError(str(exc)) from exc
    if not isinstance(body, dict):
        raise DecodeError("frame is not an object")

    name = body.get("functionName")
    if not isinstance(name, str):
        raise DecodeError("missing functionName")
    payload = body.get("payload")
    return Envelope(
        function_name=name,
        payload=payload if isinstance(payload, dict) else {},
        message_id=int(body.get("messageId") or 0),
        in_response_to=int(body.get("inResponseTo") or 0),
        sender=str(body.get("from") or ""),
        recipient=str(body.get("to") or ""),
        response_expected=bool(body.get("responseExpected")),
        at=str(body.get("timeStamp") or ""),
    )


class Ids:
    """Monotonic message ids for one side of the channel.

    Each side numbers independently, so an id is only unique per sender."""

    def __init__(self, start: int = 10_000) -> None:
        self._counter = itertools.count(start)

    def next(self) -> int:
        return next(self._counter)


def request(name: str, payload: dict[str, Any], ids: Ids, expect_reply: bool = True) -> Envelope:
    return Envelope(
        function_name=name,
        payload=payload,
        message_id=ids.next(),
        response_expected=expect_reply,
    )


def reply_to(source: Envelope, payload: dict[str, Any], ids: Ids) -> Envelope:
    """A reply echoes the request's functionName and carries its messageId.

    Correlate on name plus in_response_to; ids alone are not unique across
    senders.
    """
    return Envelope(
        function_name=source.function_name,
        payload=payload,
        message_id=ids.next(),
        in_response_to=source.message_id,
    )
