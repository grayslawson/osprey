"""Getting H.264 (AVC) out of the camera's FLV tag bodies.

This is the sibling of `hevc`. The container is identical — an FLV video tag body
of a packet type, a composition time, then length-prefixed NAL units — but the
codec is H.264 and the sequence header is an `avcC` (`AVCDecoderConfigurationRecord`)
rather than an `hvcC`. The parameter sets are just SPS and PPS; there is no VPS.

A UniFi camera can be asked to encode H.264 (its `videoCodecs` advertises it), and
some clients — Home Assistant's ONVIF integration among them — require an H.264
profile and reject an H.265-only device. So both codecs have to be handled.

The container-level helpers — `video_packet`, `split_nalus` — are codec-neutral and
live in `hevc`; reuse them from there. Only what is specific to H.264 is here: the
one-byte NAL header, IDR-as-keyframe, and the `avcC` record.

Nothing here is Ubiquiti-specific: this is ISO/IEC 14496-15 and the FLV spec.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Final

NAL_IDR: Final = 5  # a coded slice of an IDR picture — a decoder can start here
NAL_SPS: Final = 7
NAL_PPS: Final = 8

# Fixed part of an avcC record, up to but excluding the SPS count.
AVCC_FIXED_LEN: Final = 5


@dataclass(frozen=True)
class ParameterSets:
    """SPS/PPS — the parameter sets an H.264 decoder needs before the first frame.

    The surface mirrors `hevc.ParameterSets` (`complete`, `length_size`, `sets`,
    `as_annex_b`) so the media and RTSP layers can treat either codec uniformly.
    """

    sps: bytes = b""
    pps: bytes = b""
    length_size: int = 4

    @property
    def complete(self) -> bool:
        return bool(self.sps and self.pps)

    @property
    def sets(self) -> tuple[bytes, ...]:
        """The parameter-set NAL units, in the order a decoder wants them."""
        return tuple(s for s in (self.sps, self.pps) if s)

    def as_annex_b(self) -> bytes:
        start = b"\x00\x00\x00\x01"
        return b"".join(start + s for s in self.sets)

    @property
    def profile_level_id(self) -> str:
        """The three-byte profile_idc/constraints/level_idc from the SPS, as hex.

        This is what an SDP `profile-level-id` carries, and a receiver uses it to
        pick a decoder — a wrong or missing value makes some clients refuse the
        stream. The bytes follow the one-byte NAL header.
        """
        if len(self.sps) < 4:
            return ""
        return self.sps[1:4].hex()

    def sprop(self) -> str:
        """The `sprop-parameter-sets` value: base64 SPS and PPS, comma-separated."""
        return ",".join(base64.b64encode(s).decode() for s in self.sets)


def nal_type(unit: bytes) -> int | None:
    """H.264 NAL header is one byte; the type is its low five bits."""
    if not unit:
        return None
    return unit[0] & 0x1F


def is_keyframe(unit: bytes) -> bool:
    return nal_type(unit) == NAL_IDR


def parse_avcc(record: bytes) -> ParameterSets:
    """Pull SPS and PPS out of an `avcC` configuration record.

    Returns whatever was found; callers check `complete` rather than trusting a
    truncated record.
    """
    if len(record) <= AVCC_FIXED_LEN:
        return ParameterSets()
    length_size = (record[4] & 0x03) + 1
    cursor = AVCC_FIXED_LEN
    sps = b""
    pps = b""
    sps_count = record[cursor] & 0x1F
    cursor += 1
    for _ in range(sps_count):
        if cursor + 2 > len(record):
            break
        size = int.from_bytes(record[cursor : cursor + 2], "big")
        cursor += 2
        unit = record[cursor : cursor + size]
        cursor += size
        if len(unit) == size and not sps:
            sps = unit
    if cursor >= len(record):
        return ParameterSets(sps=sps, pps=pps, length_size=length_size)
    pps_count = record[cursor]
    cursor += 1
    for _ in range(pps_count):
        if cursor + 2 > len(record):
            break
        size = int.from_bytes(record[cursor : cursor + 2], "big")
        cursor += 2
        unit = record[cursor : cursor + size]
        cursor += size
        if len(unit) == size and not pps:
            pps = unit
    return ParameterSets(sps=sps, pps=pps, length_size=length_size)


def parse_length_prefixed_sets(
    payload: bytes, length_size: int, frame_length_size: int = 4
) -> ParameterSets:
    """Pull SPS/PPS out of a run of length-prefixed NAL units.

    The mirror of `hevc.parse_length_prefixed_sets`, for a camera that does not
    send a standard `avcC` and instead lays the parameter sets out as
    `[length][NAL]` in a row. `length_size` is how many bytes each NAL's length
    occupies here; the returned `length_size` is the one the *frames* use.
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
        if kind in (NAL_SPS, NAL_PPS) and kind not in found:
            found[kind] = unit
    return ParameterSets(
        sps=found.get(NAL_SPS, b""),
        pps=found.get(NAL_PPS, b""),
        length_size=frame_length_size,
    )


def parameter_sets_from_units(units: list[bytes], length_size: int = 4) -> ParameterSets:
    """Collect SPS/PPS from already-split NAL units — the *in-band* case.

    A real UVC camera does not send an H.264 sequence-header tag at all; it prepends
    the parameter sets to the keyframe as ordinary NAL units. So the parameter sets
    have to be picked out of a decoded frame's units, not a configuration record.
    """
    found: dict[int, bytes] = {}
    for unit in units:
        kind = nal_type(unit)
        if kind in (NAL_SPS, NAL_PPS) and kind not in found:
            found[kind] = unit
    return ParameterSets(
        sps=found.get(NAL_SPS, b""), pps=found.get(NAL_PPS, b""), length_size=length_size
    )


def parameter_sets(video_body: bytes) -> ParameterSets:
    """Read the parameter sets out of a video sequence-header tag body.

    Handles both shapes, exactly as `hevc.parameter_sets` does for HEVC:

    * a standard `avcC` record at the FLV/AVC payload offset (byte 5 on);
    * a bare run of 2-byte length-prefixed NAL units from byte 2, the layout a
      real UVC camera uses (it also sets byte 1 to 1 rather than 0, so config is
      told apart by the FLV frame type, not that byte).

    Whichever yields a complete set wins.
    """
    if len(video_body) > 5:
        as_avcc = parse_avcc(video_body[5:])
        if as_avcc.complete:
            return as_avcc
    return parse_length_prefixed_sets(video_body[2:], length_size=2)
