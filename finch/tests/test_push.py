"""What finch writes, cuckoo reads.

The two halves were written from the same measurements but by different code, so
running one through the other is the strongest check available without hardware:
if either drifts, these fail.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path

from unifiwire import amf
from unifiwire import flv
from unifiwire import hevc
import identity
import push
import pytest

# These tests read finch's output with cuckoo's ingest — the strongest check we
# have, and worth having, but finch does not depend on cuckoo to work.
media = pytest.importorskip("media", reason="cuckoo is not on the path")

CHANNEL = identity.CHANNELS[0]


def a_source(tmp_path: Path) -> push.Source:
    """A tiny Annex B stream: parameter sets, an IRAP, and a trailing picture."""
    vps = bytes([0x40, 0x01, 0x0C, 0x01])
    sps = bytes([0x42, 0x01, 0x01, 0x60])
    pps = bytes([0x44, 0x01, 0xC0])
    idr = bytes([0x26, 0x01]) + b"\x11" * 40
    trail = bytes([0x02, 0x01]) + b"\x22" * 20
    start = b"\x00\x00\x00\x01"
    path = tmp_path / "clip.h265"
    path.write_bytes(b"".join(start + unit for unit in (vps, sps, pps, idr, trail)))
    return push.Source(path, fps=15)


# ------------------------------------------------------------------- the trailer


def test_trailer_is_sixteen_bytes_with_the_video_clock() -> None:
    """90000 for video, 11025 otherwise — the numbers a receiver keys off."""
    video = push.trailer(True, elapsed=1.0)
    other = push.trailer(False, elapsed=1.0)
    assert len(video) == len(other) == flv.TRAILER_LEN
    assert int.from_bytes(video[1:4], "big") == 90_000
    assert int.from_bytes(other[1:4], "big") == 11_025
    assert video[0] == 0


def test_trailer_carries_elapsed_time_in_hundred_thousandths() -> None:
    assert struct.unpack(">I", push.trailer(True, elapsed=2.5)[12:16])[0] == 250_000


def test_inter_tag_gap_matches_what_cuckoo_skips() -> None:
    """finch writes 4 + 16 between tags; cuckoo's deframer expects exactly that."""
    body = b"payload"
    encoded = push.tag(flv.TagType.VIDEO, 0, body, elapsed=0.0)
    assert len(encoded) == flv.TAG_HEADER_LEN + len(body) + flv.INTER_TAG_LEN


def test_header_declares_the_extended_flag() -> None:
    assert push.header()[4] == flv.FLAGS_EXTENDED


# ----------------------------------------------------------------------- AMF0


def test_metadata_round_trips_through_cuckoos_reader() -> None:
    body = amf.script_body("onMetaData", push.metadata("video1", CHANNEL))
    assert flv.stream_name(body) == "video1"


def test_metadata_is_the_cameras_nine_keys_and_no_others() -> None:
    """Sending ffmpeg's metadata vocabulary instead leaves the receiver with no
    channel to file the stream under."""
    fields = push.metadata("alias-xyz", CHANNEL)
    assert set(fields) == {
        "audioBandwidth", "audioChannels", "audioFrequency", "channelId",
        "extendedFormat", "hasAudio", "hasVideo", "streamId", "streamName",
    }
    assert fields["channelId"] == 0.0 and fields["streamId"] == 1.0
    assert fields["streamName"] == "alias-xyz"
    assert fields["extendedFormat"] is True


def test_metadata_is_an_amf_object_not_an_ecma_array() -> None:
    body = amf.script_body("onMetaData", push.metadata("x", CHANNEL))
    marker = body[1 + 2 + len("onMetaData")]
    assert marker == amf.OBJECT, "the receiver reads an object; an array is ignored"


def test_channel_ids_match_the_receivers_filing_scheme() -> None:
    """Recordings are filed as <MAC>_<channelId>, so these ids are load bearing."""
    assert [(c.track, c.channel_id, c.stream_id) for c in identity.CHANNELS] == [
        ("video1", 0, 1), ("video2", 1, 2), ("video3", 2, 4)
    ]


def test_amf_encodes_booleans_as_booleans_not_numbers() -> None:
    """bool is an int in Python; encoding one as a number corrupts the tag."""
    encoded = amf.value(False)
    assert encoded == bytes([amf.BOOLEAN, 0])
    assert amf.value(0.0)[0] == amf.NUMBER


def test_nested_objects_are_encoded_for_mpma() -> None:
    body = amf.script_body("onMpma", push.mpma(CHANNEL))
    assert b"onMpma" in body and b"cs" in body and b"cur" in body


# -------------------------------------------------------- finch writes, cuckoo reads


def test_a_pushed_stream_is_readable_by_cuckoos_ingest(tmp_path: Path) -> None:
    hub = media.Hub()
    server = media.IngestServer(hub, port=0, fallback_name="unused")
    server.start()
    pusher = push.Pusher(
        host="127.0.0.1",
        port=int(server.server_address[1]),
        stream_name="video1",
        channel=CHANNEL,
        source=a_source(tmp_path),
        pace=False,
    )
    try:
        pusher.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            stream = hub.get("video1")
            if stream is not None and stream.ready and stream.frames >= 2:
                break
            time.sleep(0.02)
        stream = hub.get("video1")
        assert stream is not None, "cuckoo never saw the stream name finch announced"
        assert stream.ready, "cuckoo could not read the parameter sets finch sent"
        assert stream.frames >= 2 and stream.keyframes >= 1
        assert stream.parameters.length_size == 4
    finally:
        pusher.stop()
        server.stop()


def test_the_stream_name_finch_announces_is_the_one_cuckoo_routes_on(tmp_path: Path) -> None:
    hub = media.Hub()
    server = media.IngestServer(hub, port=0, fallback_name="fallback-should-not-win")
    server.start()
    pusher = push.Pusher(
        host="127.0.0.1", port=int(server.server_address[1]), stream_name="video3",
        channel=CHANNEL, source=a_source(tmp_path), pace=False,
    )
    try:
        pusher.start()
        deadline = time.time() + 10
        while time.time() < deadline and "video3" not in hub.names():
            time.sleep(0.02)
        assert hub.names() == ["video3"]
    finally:
        pusher.stop()
        server.stop()


def test_a_dead_destination_is_retried_rather_than_abandoned(tmp_path: Path) -> None:
    """A real camera keeps dialling; the controller may not be listening yet."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nothing is listening on that port now

    pusher = push.Pusher(
        host="127.0.0.1", port=port, stream_name="video1",
        channel=CHANNEL, source=a_source(tmp_path), pace=False,
    )
    pusher.start()
    try:
        time.sleep(0.3)
        assert not pusher.stopping.is_set(), "a refused connection must not end the pusher"
    finally:
        pusher.stop()


def test_stopping_a_pusher_ends_its_thread(tmp_path: Path) -> None:
    hub = media.Hub()
    server = media.IngestServer(hub, port=0, fallback_name="unused")
    server.start()
    pusher = push.Pusher(
        host="127.0.0.1", port=int(server.server_address[1]), stream_name="video1",
        channel=CHANNEL, source=a_source(tmp_path), pace=False,
    )
    try:
        pusher.start()
        time.sleep(0.2)
        pusher.stop()
        assert pusher.stopping.is_set()
        assert threading.active_count() >= 1
    finally:
        server.stop()


def test_source_refuses_a_file_with_no_parameter_sets(tmp_path: Path) -> None:
    path = tmp_path / "empty.h265"
    path.write_bytes(b"\x00\x00\x00\x01" + bytes([0x02, 0x01, 0x03]))
    try:
        push.Source(path).hvcc()
        raise AssertionError("a file with no VPS/SPS/PPS should be refused")
    except ValueError as exc:
        assert "VPS" in str(exc)


def test_pictures_are_grouped_with_their_keyframe_flag(tmp_path: Path) -> None:
    pictures = list(a_source(tmp_path).pictures())
    assert len(pictures) == 2
    assert pictures[0][1] is True, "the IRAP is a keyframe"
    assert pictures[1][1] is False
    assert all(hevc.nal_type(u) not in (32, 33, 34) for units, _ in pictures for u in units)
