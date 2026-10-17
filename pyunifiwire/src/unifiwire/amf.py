"""Just enough AMF0 to write the metadata tags a controller reads.

Script tags carry the stream's name, size and codecs. A controller reads them to
decide what it has been handed, so they are not decoration.

Only the writing side is here. Readers generally want one key out of the tag
rather than a full AMF0 parser, which `flv.stream_name` does directly.
"""

from __future__ import annotations

import struct
from typing import Any, Final

NUMBER: Final = 0x00
BOOLEAN: Final = 0x01
STRING: Final = 0x02
OBJECT: Final = 0x03
NULL: Final = 0x05
ECMA_ARRAY: Final = 0x08
OBJECT_END: Final = 0x09


def string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def value(item: Any) -> bytes:
    """Encode one AMF0 value. Bools before numbers: bool is an int in Python."""
    if isinstance(item, bool):
        return bytes([BOOLEAN, 1 if item else 0])
    if isinstance(item, (int, float)):
        return bytes([NUMBER]) + struct.pack(">d", float(item))
    if isinstance(item, str):
        return bytes([STRING]) + string(item)
    if isinstance(item, dict):
        return object_value(item)
    if item is None:
        return bytes([NULL])
    raise TypeError(f"cannot encode {type(item).__name__} as AMF0")


def object_value(properties: dict[str, Any]) -> bytes:
    out = bytearray([OBJECT])
    for key, item in properties.items():
        out += string(key) + value(item)
    out += string("") + bytes([OBJECT_END])
    return bytes(out)


def script_body(name: str, properties: dict[str, Any]) -> bytes:
    """A script tag body: the handler name, then its argument.

    The argument is an AMF0 **object** (`0x03`). Most FLV writers, ffmpeg included,
    emit an ECMA array (`0x08`) for `onMetaData` — the real camera does not, and
    this receiver reads what the camera sends.
    """
    return bytes([STRING]) + string(name) + object_value(properties)
