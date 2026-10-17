"""Getting HEVC and AAC out of the camera's FLV tag bodies.

The video bodies are shaped like FLV's AVC packets — a packet type, a composition
time, then length-prefixed NAL units — but carry HEVC, and the sequence header is
an `hvcC` configuration record rather than an `avcC` one. The parameter sets have
to come out of that record: a decoder needs them before any frame can be decoded,
and a republisher needs them before it can describe the stream.

Nothing here is Ubiquiti-specific: this is ISO/IEC 14496-15 and the FLV spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterator

PACKET_SEQUENCE_HEADER: Final = 0
PACKET_NALU: Final = 1
PACKET_END: Final = 2

NAL_VPS: Final = 32
NAL_SPS: Final = 33
NAL_PPS: Final = 34
NAL_PREFIX_SEI: Final = 39
NAL_SUFFIX_SEI: Final = 40

IRAP_RANGE: Final = range(16, 24)  # BLA/IDR/CRA — a decoder can start here

# Fixed part of an hvcC record, up to but excluding numOfArrays.
HVCC_FIXED_LEN: Final = 22

SOUND_FORMAT_AAC: Final = 10
AAC_SEQUENCE_HEADER: Final = 0
AAC_RAW: Final = 1


@dataclass(frozen=True)
class VideoPacket:
    """The FLV video tag body, unwrapped."""

    packet_type: int
    composition_time: int
    payload: bytes

    @property
    def is_config(self) -> bool:
        return self.packet_type == PACKET_SEQUENCE_HEADER


@dataclass(frozen=True)
class ParameterSets:
    """VPS/SPS/PPS — the parameter sets a decoder needs before the first frame."""

    vps: bytes = b""
    sps: bytes = b""
    pps: bytes = b""
    length_size: int = 4

    @property
    def complete(self) -> bool:
        return bool(self.vps and self.sps and self.pps)

    @property
    def sets(self) -> tuple[bytes, ...]:
        """The parameter-set NAL units, in the order a decoder wants them.

        Mirrors `avc.ParameterSets.sets` so the media and RTSP layers can prepend
        the sets to a keyframe without knowing which codec they hold.
        """
        return tuple(s for s in (self.vps, self.sps, self.pps) if s)

    def as_annex_b(self) -> bytes:
        start = b"\x00\x00\x00\x01"
        return b"".join(start + s for s in self.sets)


def video_packet(body: bytes) -> VideoPacket | None:
    """Unwrap a video tag body. The first byte is frame type and codec id."""
    if len(body) < 5:
        return None
    composition = int.from_bytes(body[2:5], "big", signed=True)
    return VideoPacket(packet_type=body[1], composition_time=composition, payload=body[5:])


def parse_hvcc(record: bytes) -> ParameterSets:
    """Pull the parameter sets out of an hvcC configuration record.

    Returns whatever was found; callers check `complete` rather than trusting a
    truncated record.
    """
    if len(record) <= HVCC_FIXED_LEN:
        return ParameterSets()
    length_size = (record[21] & 0x03) + 1
    found: dict[int, bytes] = {}
    cursor = HVCC_FIXED_LEN
    arrays = record[cursor]
    cursor += 1
    for _ in range(arrays):
        if cursor + 3 > len(record):
            break
        nal_type = record[cursor] & 0x3F
        count = int.from_bytes(record[cursor + 1 : cursor + 3], "big")
        cursor += 3
        for _ in range(count):
            if cursor + 2 > len(record):
                break
            size = int.from_bytes(record[cursor : cursor + 2], "big")
            cursor += 2
            unit = record[cursor : cursor + size]
            cursor += size
            if len(unit) == size and nal_type not in found:
                found[nal_type] = unit
    return ParameterSets(
        vps=found.get(NAL_VPS, b""),
        sps=found.get(NAL_SPS, b""),
        pps=found.get(NAL_PPS, b""),
        length_size=length_size,
    )


def parse_length_prefixed_sets(
    payload: bytes, length_size: int, frame_length_size: int = 4
) -> ParameterSets:
    """Pull parameter sets out of a run of length-prefixed NAL units.

    Some cameras do not send a standard hvcC record. Instead the sequence header
    carries the parameter sets as `[length][NAL]` in a row — the same shape an
    hvcC array uses, but without the fixed configuration prefix. `length_size` is
    how many bytes each NAL's length occupies here (2 in practice); the returned
    `length_size` is the one the *frames* use, which is not necessarily the same.
    """
    found: dict[int, bytes] = {}
    cursor = 0
    while cursor + length_size <= len(payload):
        size = int.from_bytes(payload[cursor : cursor + length_size], "big")
        cursor += length_size
        unit = payload[cursor : cursor + size]
        cursor += size
        if size <= 0 or len(unit) != size:
            break
        kind = nal_type(unit)
        if kind in (NAL_VPS, NAL_SPS, NAL_PPS) and kind not in found:
            found[kind] = unit
    return ParameterSets(
        vps=found.get(NAL_VPS, b""),
        sps=found.get(NAL_SPS, b""),
        pps=found.get(NAL_PPS, b""),
        length_size=frame_length_size,
    )


def parameter_sets(video_body: bytes) -> ParameterSets:
    """Read the parameter sets out of a video sequence-header tag body.

    Handles both shapes seen in the wild:

    * a standard `hvcC` record, at the FLV/AVC payload offset (byte 5 on) — what
      most writers, and this library's own, produce;
    * a bare run of 2-byte length-prefixed NAL units starting at byte 2, which is
      what a real UVC camera sends. It also sets `byte 1` to 1 rather than the
      standard 0, so config cannot be told apart by that byte — the caller keys on
      the FLV frame type instead.

    Whichever yields a complete set wins; an incomplete first attempt falls
    through to the second rather than being trusted.
    """
    if len(video_body) > 5:
        as_hvcc = parse_hvcc(video_body[5:])
        if as_hvcc.complete:
            return as_hvcc
    # The camera's own layout: skip the two-byte header, read 2-byte-prefixed NALs.
    return parse_length_prefixed_sets(video_body[2:], length_size=2)


def parameter_sets_from_units(units: list[bytes], length_size: int = 4) -> ParameterSets:
    """Collect VPS/SPS/PPS from already-split NAL units — the *in-band* case.

    Mirrors `avc.parameter_sets_from_units`. HEVC on this wire does send a sequence
    header, but a stream can also repeat the sets in band ahead of a keyframe, and
    recovering them from a frame's units makes a late joiner robust either way.
    """
    found: dict[int, bytes] = {}
    for unit in units:
        kind = nal_type(unit)
        if kind in (NAL_VPS, NAL_SPS, NAL_PPS) and kind not in found:
            found[kind] = unit
    return ParameterSets(
        vps=found.get(NAL_VPS, b""),
        sps=found.get(NAL_SPS, b""),
        pps=found.get(NAL_PPS, b""),
        length_size=length_size,
    )


def split_nalus(payload: bytes, length_size: int = 4) -> Iterator[bytes]:
    """Walk length-prefixed NAL units, stopping cleanly on a truncated tail."""
    cursor = 0
    while cursor + length_size <= len(payload):
        size = int.from_bytes(payload[cursor : cursor + length_size], "big")
        cursor += length_size
        if size <= 0 or cursor + size > len(payload):
            return
        yield payload[cursor : cursor + size]
        cursor += size


def nal_type(unit: bytes) -> int | None:
    """HEVC NAL header is two bytes; the type is six bits of the first."""
    if not unit:
        return None
    return (unit[0] >> 1) & 0x3F


def is_irap(unit: bytes) -> bool:
    kind = nal_type(unit)
    return kind is not None and kind in IRAP_RANGE


@dataclass(frozen=True)
class AudioPacket:
    """The FLV audio tag body, unwrapped. Only AAC is expected on this wire."""

    is_aac: bool
    is_config: bool
    payload: bytes


def audio_packet(body: bytes) -> AudioPacket | None:
    if not body:
        return None
    fmt = body[0] >> 4
    if fmt != SOUND_FORMAT_AAC:
        return AudioPacket(is_aac=False, is_config=False, payload=body[1:])
    if len(body) < 2:
        return None
    return AudioPacket(
        is_aac=True,
        is_config=body[1] == AAC_SEQUENCE_HEADER,
        payload=body[2:],
    )


def audio_config(asc: bytes) -> tuple[int, int, int] | None:
    """Read object type, sample rate and channels out of an AudioSpecificConfig.

    Needed for the SDP's `config=` parameter and to state the clock rate, which a
    receiver cannot guess: this wire carries 16 kHz mono, not the 44.1 kHz default
    an RTP receiver would otherwise assume.
    """
    if len(asc) < 2:
        return None
    rates = (
        96000, 88200, 64000, 48000, 44100, 32000,
        24000, 22050, 16000, 12000, 11025, 8000, 7350,
    )
    object_type = asc[0] >> 3
    rate_index = ((asc[0] & 0x07) << 1) | (asc[1] >> 7)
    channels = (asc[1] >> 3) & 0x0F
    if rate_index >= len(rates):
        return None
    return object_type, rates[rate_index], channels
