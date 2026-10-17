"""Annex B streams, and the `hvcC` record that describes one.

A camera sends HEVC as length-prefixed NAL units inside FLV, with the parameter
sets held in an `hvcC` configuration record. Files on disk — anything out of
`ffmpeg -f hevc` — are Annex B: the same NAL units separated by start codes.
Converting between the two is the whole job here.
"""

from __future__ import annotations

from typing import Final, Iterator

from . import avc, hevc

START_CODE: Final = b"\x00\x00\x01"
VCL_MAX: Final = 31  # HEVC NAL types 0..31 are coded slices


def annex_b_units(data: bytes) -> Iterator[bytes]:
    """Split an Annex B stream on start codes, three- or four-byte."""
    at = data.find(START_CODE)
    while at >= 0:
        start = at + len(START_CODE)
        following = data.find(START_CODE, start)
        end = len(data) if following < 0 else following
        unit = data[start:end]
        if unit.endswith(b"\x00"):
            unit = unit.rstrip(b"\x00")  # trailing zero belongs to the next start code
        if unit:
            yield unit
        at = following


def build_hvcc(vps: bytes, sps: bytes, pps: bytes) -> bytes:
    """The smallest hvcC record that carries the parameter sets and length size."""
    record = bytearray(hevc.HVCC_FIXED_LEN)
    record[0] = 1
    if len(sps) > 3:
        record[1] = sps[3]  # profile space / tier / profile idc, copied from the SPS
    record[21] = 0xFC | 0x03  # four-byte NAL lengths
    record.append(3)
    for nal_type, unit in ((hevc.NAL_VPS, vps), (hevc.NAL_SPS, sps), (hevc.NAL_PPS, pps)):
        record.append(0x80 | nal_type)  # array complete
        record += (1).to_bytes(2, "big")
        record += len(unit).to_bytes(2, "big")
        record += unit
    return bytes(record)


def frames(units: list[bytes]) -> Iterator[tuple[list[bytes], bool]]:
    """Group NAL units into frames, keeping non-VCL units with the frame they precede."""
    pending: list[bytes] = []
    for unit in units:
        kind = hevc.nal_type(unit)
        if kind is None:
            continue
        pending.append(unit)
        if kind <= VCL_MAX:
            yield pending, hevc.is_irap(unit)
            pending = []
    if pending:
        yield pending, False


def build_avcc(sps: bytes, pps: bytes, length_size: int = 4) -> bytes:
    """The smallest `avcC` record that carries one SPS, one PPS and the length size.

    The mirror of `build_hvcc` for H.264. The three bytes after the SPS NAL header
    are profile_idc / constraints / level_idc, and they are copied into the record
    so a receiver reads a consistent profile.
    """
    if len(sps) < 4:
        raise ValueError("SPS too short to describe a profile")
    record = bytearray([1, sps[1], sps[2], sps[3], 0xFC | (length_size - 1), 0xE0 | 1])
    record += len(sps).to_bytes(2, "big") + sps
    record.append(1)
    record += len(pps).to_bytes(2, "big") + pps
    return bytes(record)


AVC_VCL_RANGE: Final = range(1, 6)  # H.264 NAL types 1..5 are coded slices


def avc_frames(units: list[bytes]) -> Iterator[tuple[list[bytes], bool]]:
    """`frames`, for H.264: a one-byte NAL header, IDR (type 5) is the keyframe."""
    pending: list[bytes] = []
    for unit in units:
        kind = avc.nal_type(unit)
        if kind is None:
            continue
        pending.append(unit)
        if kind in AVC_VCL_RANGE:
            yield pending, kind == avc.NAL_IDR
            pending = []
    if pending:
        yield pending, False
