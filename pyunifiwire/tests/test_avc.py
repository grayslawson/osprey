"""H.264 (AVC) bitstream helpers — the sibling of the HEVC ones.

These test the wire, not either end of it: pulling SPS/PPS out of both the standard
`avcC` record and the bare length-prefixed layout a real UVC camera sends, and the
NAL classification the RTP layer needs.
"""

from __future__ import annotations

import base64

from unifiwire import avc

# Minimal but well-formed NAL units: the header byte carries the type in its low
# five bits (SPS 7, PPS 8, IDR 5, non-IDR slice 1). The three bytes after an SPS
# header are profile_idc / constraints / level_idc.
SPS = bytes([0x67, 0x64, 0x00, 0x1F, 0xAC, 0xD9])
PPS = bytes([0x68, 0xEE, 0x3C, 0x80])
IDR = bytes([0x65, 0x88, 0x84, 0x00])
SLICE = bytes([0x41, 0x9A, 0x00])


def avcc(sps: bytes, pps: bytes, length_size: int = 4) -> bytes:
    """A standard AVCDecoderConfigurationRecord carrying one SPS and one PPS."""
    rec = bytearray([1, sps[1], sps[2], sps[3], 0xFC | (length_size - 1), 0xE0 | 1])
    rec += len(sps).to_bytes(2, "big") + sps
    rec.append(1)
    rec += len(pps).to_bytes(2, "big") + pps
    return bytes(rec)


def video_body(record: bytes, packet_type: int = 1) -> bytes:
    """A sequence-header FLV video body: frame-type|codec, packet type, then record."""
    return bytes([(6 << 4) | 7, packet_type, 0, 0, 0]) + record


def test_parse_avcc_round_trip() -> None:
    params = avc.parameter_sets(video_body(avcc(SPS, PPS)))
    assert params.complete
    assert params.sps == SPS and params.pps == PPS
    assert params.length_size == 4
    assert params.sets == (SPS, PPS)


def test_two_byte_length_size_is_read_from_the_record() -> None:
    params = avc.parse_avcc(avcc(SPS, PPS, length_size=2))
    assert params.length_size == 2


def test_bare_length_prefixed_layout_is_the_camera_fallback() -> None:
    """No avcC: the parameter sets follow byte 2 as [u16 length][NAL], and byte 1
    is 1 rather than 0 — the same quirk the camera has for HEVC."""
    body = bytearray([(6 << 4) | 7, 1])
    for unit in (SPS, PPS):
        body += len(unit).to_bytes(2, "big") + unit
    params = avc.parameter_sets(bytes(body))
    assert params.complete
    assert params.sps == SPS and params.pps == PPS
    assert params.length_size == 4, "frames use four-byte lengths even here"


def test_parameter_sets_recovered_from_in_band_units() -> None:
    """The camera prepends SPS/PPS to the keyframe rather than sending an avcC."""
    params = avc.parameter_sets_from_units([SPS, PPS, IDR])
    assert params.complete and params.sets == (SPS, PPS)
    assert params.length_size == 4
    assert not avc.parameter_sets_from_units([IDR]).complete


def test_incomplete_record_is_not_complete() -> None:
    only_sps = bytearray([1, SPS[1], SPS[2], SPS[3], 0xFF, 0xE0 | 1])
    only_sps += len(SPS).to_bytes(2, "big") + SPS
    only_sps.append(0)  # zero PPS
    params = avc.parse_avcc(bytes(only_sps))
    assert not params.complete


def test_nal_type_reads_the_low_five_bits() -> None:
    assert avc.nal_type(SPS) == avc.NAL_SPS
    assert avc.nal_type(PPS) == avc.NAL_PPS
    assert avc.nal_type(IDR) == avc.NAL_IDR
    assert avc.nal_type(b"") is None


def test_only_an_idr_slice_is_a_keyframe() -> None:
    assert avc.is_keyframe(IDR)
    assert not avc.is_keyframe(SLICE)


def test_profile_level_id_is_the_three_sps_bytes() -> None:
    params = avc.ParameterSets(sps=SPS, pps=PPS)
    assert params.profile_level_id == SPS[1:4].hex()
    assert avc.ParameterSets().profile_level_id == ""


def test_sprop_is_comma_joined_base64() -> None:
    params = avc.ParameterSets(sps=SPS, pps=PPS)
    expected = f"{base64.b64encode(SPS).decode()},{base64.b64encode(PPS).decode()}"
    assert params.sprop() == expected


def test_as_annex_b_prefixes_start_codes() -> None:
    params = avc.ParameterSets(sps=SPS, pps=PPS)
    assert params.as_annex_b() == b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x00\x01" + PPS
