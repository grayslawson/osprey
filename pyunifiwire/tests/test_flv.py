"""The extendedFlv container.

These moved here from the two implementations that used to own them: they test the
wire itself, not either end of it.
"""

from __future__ import annotations

from unifiwire import flv

# ---------------------------------------------------------------------------- flv


def _tag(kind: int, body: bytes, timestamp: int = 0) -> bytes:
    """Build one container tag: header, body, previous-size, wall-clock trailer."""
    out = bytearray([kind])
    out += len(body).to_bytes(3, "big")
    out += (timestamp & 0xFFFFFF).to_bytes(3, "big")
    out.append(0)
    out += (0).to_bytes(3, "big")
    out += body
    out += (flv.TAG_HEADER_LEN + len(body)).to_bytes(4, "big")
    out += b"\x11" * flv.TRAILER_LEN
    return bytes(out)


def _header(flags: int = flv.FLAGS_EXTENDED) -> bytes:
    return b"FLV" + bytes([0x01, flags, 0, 0, 0, 9]) + b"\x00\x00\x00\x00"


def test_deframer_reads_flags_and_tags() -> None:
    d = flv.Deframer()
    stream = _header() + _tag(9, b"\x18\x01\xaa") + _tag(8, b"\xaf\x01\xbb")
    tags = list(d.feed(stream))
    assert d.flags == flv.FLAGS_EXTENDED
    assert [t.kind for t in tags] == [flv.TagType.VIDEO, flv.TagType.AUDIO]


def test_deframer_survives_split_reads() -> None:
    stream = _header() + _tag(9, b"\x18" + b"\x00" * 40)
    d = flv.Deframer()
    tags: list[flv.Tag] = []
    for i in range(0, len(stream), 7):  # deliberately awkward boundaries
        tags.extend(d.feed(stream[i : i + 7]))
    assert len(tags) == 1
    assert len(tags[0].body) == 41


def test_video_tag_exposes_codec_and_frame_type() -> None:
    d = flv.Deframer()
    tags = list(d.feed(_header() + _tag(9, bytes([0x68]) + b"\x01\x00")))
    tag = tags[0]
    assert tag.codec_id == flv.CODEC_H265
    assert tag.is_sequence_header
    assert not tag.is_keyframe


def test_keyframe_detection() -> None:
    d = flv.Deframer()
    tags = list(d.feed(_header() + _tag(9, bytes([0x18]) + b"\x01")))
    assert tags[0].is_keyframe
    assert tags[0].codec_id == flv.CODEC_H265


def test_inter_tag_gap_is_twenty_bytes() -> None:
    """Reading only 4 bytes between tags would desynchronise after the first."""
    d = flv.Deframer()
    stream = _header() + b"".join(_tag(9, b"\x18\x01" + bytes([n])) for n in range(5))
    assert len(list(d.feed(stream))) == 5


def test_stream_name_extracted_from_script_body() -> None:
    name = b"OQ5tZVhptKAFSkWG"
    body = b"\x00onMetaData" + b"streamName" + b"\x02" + len(name).to_bytes(2, "big") + name
    assert flv.stream_name(body) == name.decode()


def test_stream_name_absent_returns_none() -> None:
    assert flv.stream_name(b"onMetaData whatever") is None


def test_to_standard_flv_sets_conventional_flags() -> None:
    tags = [flv.Tag(flv.TagType.VIDEO, 0, b"\x18\x01")]
    out = flv.to_standard_flv(tags)
    assert out[:3] == b"FLV"
    assert out[4] == 0x05


# --- HEVC parameter-set extraction, both container shapes seen in the wild -------

from unifiwire import annexb, hevc


def _config_body(prefix: bytes, payload: bytes) -> bytes:
    """A video sequence-header tag body: FLV byte, packet byte, then the payload."""
    return prefix + payload


def test_parameter_sets_read_a_standard_hvcc() -> None:
    """What this library and most writers produce: byte1 = 0, hvcC from byte 5."""
    hvcc = annexb.build_hvcc(bytes([0x40, 0x01, 0x0C]), bytes([0x42, 0x01, 0x01]), bytes([0x44, 0x01, 0xC0]))
    body = bytes([0x68, 0x00, 0, 0, 0]) + hvcc
    sets = hevc.parameter_sets(body)
    assert sets.complete
    assert hevc.nal_type(sets.vps) == hevc.NAL_VPS
    assert sets.length_size == 4


def test_parameter_sets_read_a_length_prefixed_config() -> None:
    """What a real UVC camera sends: byte1 = 1, then 2-byte-length-prefixed NALs
    from byte 2, and no hvcC fixed header at all."""
    vps = bytes([0x40, 0x01]) + b"\x0c" * 22
    sps = bytes([0x42, 0x01]) + b"\x60" * 40
    pps = bytes([0x44, 0x01]) + b"\xe0" * 5
    payload = b"".join(len(n).to_bytes(2, "big") + n for n in (vps, sps, pps))
    body = bytes([0x68, 0x01]) + payload
    sets = hevc.parameter_sets(body)
    assert sets.complete, "the camera's own container must be understood, not just hvcC"
    assert sets.vps == vps and sets.sps == sps and sets.pps == pps
    assert sets.length_size == 4, "frames use 4-byte lengths even when config uses 2"


def test_the_packet_type_byte_does_not_decide_config() -> None:
    """The real camera sets byte1 to 1 on the sequence header too, so a reader that
    keys on it (the AVC convention) misses the parameter sets."""
    vps = bytes([0x40, 0x01, 0x0C])
    sps = bytes([0x42, 0x01, 0x60])
    pps = bytes([0x44, 0x01, 0xE0])
    payload = b"".join(len(n).to_bytes(2, "big") + n for n in (vps, sps, pps))
    body = bytes([0x68, 0x01]) + payload
    packet = hevc.video_packet(body)
    assert packet is not None and not packet.is_config, "byte1=1 reads as a frame"
    assert hevc.parameter_sets(body).complete, "but the sets are still recovered"
