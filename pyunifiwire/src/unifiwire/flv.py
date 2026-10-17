"""Deframer for the camera's push container.

It opens with an FLV signature but is not standard FLV: there are 20 bytes
between tags where FLV has 4 (a previous-tag-size plus a 16-byte wall-clock
trailer), and the header flags byte is 0x07.

Video arrives as H.265 under FLV codec id 8, which no standard demuxer maps.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Final, Iterator

HEADER_LEN: Final = 9
TAG_HEADER_LEN: Final = 11
TRAILER_LEN: Final = 16  # wall clock, after the previous-tag-size
PREV_SIZE_LEN: Final = 4
INTER_TAG_LEN: Final = PREV_SIZE_LEN + TRAILER_LEN

SIGNATURE: Final = b"FLV"
FLAGS_EXTENDED: Final = 0x07

CODEC_H264: Final = 7
CODEC_H265: Final = 8  # Ubiquiti's id for HEVC in this container


class TagType(IntEnum):
    AUDIO = 8
    VIDEO = 9
    SCRIPT = 18


class FrameType(IntEnum):
    KEY = 1
    INTER = 2
    SEQUENCE_HEADER = 6


@dataclass(frozen=True)
class Tag:
    kind: TagType
    timestamp: int
    body: bytes

    @property
    def codec_id(self) -> int | None:
        if self.kind is not TagType.VIDEO or not self.body:
            return None
        return self.body[0] & 0x0F

    @property
    def frame_type(self) -> int | None:
        if self.kind is not TagType.VIDEO or not self.body:
            return None
        return self.body[0] >> 4

    @property
    def is_sequence_header(self) -> bool:
        return self.frame_type == FrameType.SEQUENCE_HEADER

    @property
    def is_keyframe(self) -> bool:
        return self.frame_type == FrameType.KEY


def stream_name(script_body: bytes) -> str | None:
    """Pull streamName out of an AMF0 onMetaData body.

    Keys are bare AMF0 strings; the value follows as marker 0x02, a u16 length,
    then bytes. The name is assigned per session, so it is the only reliable way
    to tell concurrent tracks apart.
    """
    marker = b"streamName"
    at = script_body.find(marker)
    if at < 0:
        return None
    cursor = at + len(marker)
    if cursor >= len(script_body) or script_body[cursor] != 0x02:
        return None
    if cursor + 3 > len(script_body):
        return None
    length = int.from_bytes(script_body[cursor + 1 : cursor + 3], "big")
    value = script_body[cursor + 3 : cursor + 3 + length]
    if len(value) != length:
        return None
    return value.decode("utf-8", errors="replace")


class Deframer:
    """Feed it bytes as they arrive; take whole tags out.

    A tag routinely straddles two reads, so nothing is emitted until the full
    tag plus its trailing 20 bytes are buffered.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._started = False
        self.flags: int | None = None

    def feed(self, chunk: bytes) -> Iterator[Tag]:
        self._buffer.extend(chunk)
        if not self._started:
            if not self._consume_header():
                return
        while True:
            tag = self._take_tag()
            if tag is None:
                return
            yield tag

    def _consume_header(self) -> bool:
        at = self._buffer.find(SIGNATURE)
        if at < 0:
            # Drop everything but a possible partial signature.
            if len(self._buffer) > len(SIGNATURE):
                del self._buffer[: -len(SIGNATURE)]
            return False
        needed = at + HEADER_LEN + PREV_SIZE_LEN
        if len(self._buffer) < needed:
            return False
        self.flags = self._buffer[at + 4]
        del self._buffer[:needed]
        self._started = True
        return True

    def _take_tag(self) -> Tag | None:
        if len(self._buffer) < TAG_HEADER_LEN:
            return None
        size = int.from_bytes(self._buffer[1:4], "big")
        total = TAG_HEADER_LEN + size + INTER_TAG_LEN
        if size <= 0 or len(self._buffer) < total:
            return None
        try:
            kind = TagType(self._buffer[0])
        except ValueError:
            # Unknown tag type: skip it rather than abandoning the stream.
            del self._buffer[:total]
            return self._take_tag()
        timestamp = int.from_bytes(self._buffer[4:7], "big") | (self._buffer[7] << 24)
        body = bytes(self._buffer[TAG_HEADER_LEN : TAG_HEADER_LEN + size])
        del self._buffer[:total]
        return Tag(kind=kind, timestamp=timestamp, body=body)


def to_standard_flv(tags: list[Tag]) -> bytes:
    """Re-emit tags as standard FLV: flags 0x05, recomputed sizes, no trailer.

    Only useful for tools that accept the codec ids present; H.265 as id 8 will
    not be understood by a stock demuxer.
    """
    out = bytearray(SIGNATURE)
    out += bytes([0x01, 0x05, 0x00, 0x00, 0x00, HEADER_LEN])
    out += (0).to_bytes(PREV_SIZE_LEN, "big")
    for tag in tags:
        size = len(tag.body)
        out.append(int(tag.kind))
        out += size.to_bytes(3, "big")
        out += (tag.timestamp & 0xFFFFFF).to_bytes(3, "big")
        out.append((tag.timestamp >> 24) & 0xFF)
        out += (0).to_bytes(3, "big")
        out += tag.body
        out += (TAG_HEADER_LEN + size).to_bytes(PREV_SIZE_LEN, "big")
    return bytes(out)
