"""RTSP session hardening: interleaved RTCP, sender reports, RTP-Info, timeline
continuity across a camera reconnect, and slow-client isolation.

Everything runs over loopback against fake peers: the "camera" is the test
calling `Stream.accept`, the "players" are minimal RTSP clients below.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from unifiwire import flv
from unifiwire import hevc
import media
import rtsp
import videofmt
from test_media import (
    AAC_ASC,
    AAC_FRAME,
    IDR,
    SLICE,
    audio_body,
    hvcc,
    length_prefixed,
    video_body,
)


# --------------------------------------------------------------------- helpers


def receiver_report(reporter: int, about: int, fraction: int = 64, lost: int = 3, jitter: int = 900) -> bytes:
    """An RTCP RR with one report block, as ffmpeg sends over TCP."""
    block = struct.pack("!IIIIII", about, (fraction << 24) | lost, 1234, jitter, 0, 0)
    return struct.pack("!BBHI", 0x81, rtsp.RTCP_RR, 7, reporter) + block


def request_bytes(method: str, uri: str, cseq: int, headers: dict[str, str] | None = None, body: bytes = b"") -> bytes:
    lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def keyframe_tag(ts: int, size: int = 0) -> flv.Tag:
    unit = IDR + bytes(size)
    return flv.Tag(flv.TagType.VIDEO, ts, video_body(flv.FrameType.KEY, hevc.PACKET_NALU, length_prefixed(unit)))


def inter_tag(ts: int, size: int = 0) -> flv.Tag:
    unit = SLICE + bytes(size)
    return flv.Tag(flv.TagType.VIDEO, ts, video_body(flv.FrameType.INTER, hevc.PACKET_NALU, length_prefixed(unit)))


def audio_tag(ts: int) -> flv.Tag:
    return flv.Tag(flv.TagType.AUDIO, ts, audio_body(hevc.AAC_RAW, AAC_FRAME))


def live_stream(hub: media.Hub, name: str = "video1", with_audio: bool = True) -> media.Stream:
    stream = hub.stream(name)
    stream.accept(flv.Tag(flv.TagType.VIDEO, 0, video_body(6, hevc.PACKET_SEQUENCE_HEADER, hvcc())))
    if with_audio:
        stream.accept(flv.Tag(flv.TagType.AUDIO, 0, audio_body(hevc.AAC_SEQUENCE_HEADER, AAC_ASC)))
    stream.accept(keyframe_tag(0))  # a frame has arrived: the track is live
    return stream


class Player:
    """A minimal TCP-interleaved RTSP client that keeps channel numbers."""

    def __init__(self, port: int, rcvbuf: int | None = None) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf is not None:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.settimeout(5)
        self.sock.connect(("127.0.0.1", port))
        self.base = f"rtsp://127.0.0.1:{port}"
        self.cseq = 0
        self.reader = _ClientReader()
        self.media: list[tuple[int, bytes]] = []
        self.replies: list[str] = []
        self.session = ""

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> str:
        self.cseq += 1
        headers = dict(headers or {})
        if self.session and "Session" not in headers:
            headers["Session"] = self.session
        self.sock.sendall(request_bytes(method, self.base + path, self.cseq, headers))
        return self.reply()

    def reply(self, timeout: float = 5.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.replies:
                return self.replies.pop(0)
            self._fill(deadline)
        raise AssertionError("no RTSP reply")

    def channel(self, channel: int, timeout: float = 5.0) -> bytes | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for index, (got, payload) in enumerate(self.media):
                if got == channel:
                    del self.media[index]
                    return payload
            if not self._fill(deadline):
                if self.closed:
                    return None
        return None

    closed = False

    def _fill(self, deadline: float) -> bool:
        self.sock.settimeout(max(0.05, deadline - time.time()))
        try:
            chunk = self.sock.recv(65536)
        except (TimeoutError, OSError):
            return False
        if not chunk:
            self.closed = True
            return False
        for kind, channel, payload in self.reader.feed(chunk):
            if kind == "rtsp":
                self.replies.append(payload.decode())
            else:
                self.media.append((channel, payload))
        return True

    def play(self, path: str = "/video1", audio: bool = True) -> str:
        assert "200 OK" in self.request("DESCRIBE", path)
        setup = self.request(
            "SETUP", f"{path}/trackID=0", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"}
        )
        assert "200 OK" in setup, setup
        self.session = header(setup, "session").split(";")[0]
        if audio:
            setup = self.request(
                "SETUP", f"{path}/trackID=1", {"Transport": "RTP/AVP/TCP;unicast;interleaved=2-3"}
            )
            assert "200 OK" in setup, setup
        played = self.request("PLAY", path)
        assert "200 OK" in played, played
        return played

    def close(self) -> None:
        self.sock.close()


class _ClientReader:
    def __init__(self) -> None:
        self.buffer = b""

    def feed(self, data: bytes) -> list[tuple[str, int, bytes]]:
        self.buffer += data
        out: list[tuple[str, int, bytes]] = []
        while self.buffer:
            if self.buffer[:1] == b"$":
                if len(self.buffer) < 4:
                    break
                size = int.from_bytes(self.buffer[2:4], "big")
                if len(self.buffer) < 4 + size:
                    break
                out.append(("rtp", self.buffer[1], self.buffer[4 : 4 + size]))
                self.buffer = self.buffer[4 + size :]
                continue
            if b"\r\n\r\n" not in self.buffer:
                break
            head, _, rest = self.buffer.partition(b"\r\n\r\n")
            length = 0
            for line in head.decode().split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            if len(rest) < length:
                break
            out.append(("rtsp", -1, head + b"\r\n\r\n" + rest[:length]))
            self.buffer = rest[length:]
        return out


def header(reply: str, name: str) -> str:
    for line in reply.split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() == name:
            return value.strip()
    raise AssertionError(f"no {name} header in {reply!r}")


def rtp_fields(packet: bytes) -> tuple[int, int, int, int]:
    """(payload type, sequence, timestamp, ssrc)"""
    _, second, seq, ts, ssrc = struct.unpack("!BBHII", packet[:12])
    return second & 0x7F, seq, ts, ssrc


def track_stats(server: rtsp.RtspServer, name: str = "video1", kind: str = "video") -> dict[str, object]:
    clients = server.client_stats().get(name, [])
    if not clients:
        return {}
    tracks = clients[0]["tracks"]
    assert isinstance(tracks, dict)
    track = tracks.get(kind, {})
    assert isinstance(track, dict)
    return track


@contextmanager
def serving(hub: media.Hub) -> Iterator[rtsp.RtspServer]:
    server = rtsp.RtspServer(hub, advertise_host="127.0.0.1", port=0, bind_host="127.0.0.1")
    server.start()
    try:
        yield server
    finally:
        server.stop()


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------- the control-stream reader


def test_binary_rtcp_and_text_requests_are_demultiplexed() -> None:
    rr = receiver_report(1, 2)
    # An RR whose bytes look like the end of a header and a request line must
    # still be taken by length, not scanned as text.
    tricky = b"\r\n\r\nOPTIONS x RTSP/1.0"
    stream = (
        rtsp.interleaved_frame(1, rr)
        + request_bytes("GET_PARAMETER", "rtsp://h/video1", 5, {"Session": "AB"})
        + rtsp.interleaved_frame(3, tricky)
        + request_bytes("OPTIONS", "rtsp://h/video1", 6)
    )
    reader = rtsp.ControlReader()
    items: list[rtsp.Request | rtsp.Interleaved] = []
    for index in range(len(stream)):  # worst case: one byte per recv
        items += reader.feed(stream[index : index + 1])
    assert [type(i).__name__ for i in items] == ["Interleaved", "Request", "Interleaved", "Request"]
    first, second, third, fourth = items
    assert isinstance(first, rtsp.Interleaved) and first.channel == 1 and first.payload == rr
    assert isinstance(second, rtsp.Request) and second.method == "GET_PARAMETER" and second.cseq == "5"
    assert isinstance(third, rtsp.Interleaved) and third.payload == tricky
    assert isinstance(fourth, rtsp.Request) and fourth.method == "OPTIONS"
    assert reader.pending == 0


def test_request_bodies_are_consumed_by_content_length() -> None:
    reader = rtsp.ControlReader()
    data = request_bytes("GET_PARAMETER", "rtsp://h/v", 1, body=b"$\x00ping") + request_bytes("OPTIONS", "rtsp://h/v", 2)
    items = reader.feed(data)
    assert len(items) == 2
    assert isinstance(items[0], rtsp.Request) and items[0].body == b"$\x00ping"


def test_stray_bytes_between_messages_are_skipped() -> None:
    reader = rtsp.ControlReader()
    items = reader.feed(b"\r\n\x00\x01" + request_bytes("OPTIONS", "rtsp://h/v", 1))
    assert len(items) == 1 and isinstance(items[0], rtsp.Request) and reader.skipped == 4


def test_an_unbounded_header_is_a_protocol_error() -> None:
    reader = rtsp.ControlReader(max_header=64)
    with pytest.raises(rtsp.ProtocolError):
        reader.feed(b"OPTIONS " + b"a" * 100)


def test_an_oversized_body_is_a_protocol_error() -> None:
    reader = rtsp.ControlReader(max_body=4)
    with pytest.raises(rtsp.ProtocolError):
        reader.feed(b"SET_PARAMETER x RTSP/1.0\r\nContent-Length: 99\r\n\r\n")


def test_partial_interleaved_frames_wait_for_the_rest() -> None:
    reader = rtsp.ControlReader()
    framed = rtsp.interleaved_frame(1, b"abcdef")
    assert reader.feed(framed[:5]) == []
    items = reader.feed(framed[5:])
    assert items == [rtsp.Interleaved(channel=1, payload=b"abcdef")]


# ------------------------------------------------------------------------ RTCP


def test_receiver_reports_are_parsed() -> None:
    summary = rtsp.parse_rtcp(receiver_report(7, 0xDEADBEEF, fraction=128, lost=0xFFFFFF, jitter=450))
    assert len(summary.reports) == 1
    report = summary.reports[0]
    assert report.ssrc == 0xDEADBEEF and report.fraction_lost == 0.5
    assert report.cumulative_lost == -1, "24-bit signed"
    assert report.jitter == 450 and not summary.bye


def test_malformed_rtcp_is_not_a_crash() -> None:
    assert rtsp.parse_rtcp(b"").reports == []
    assert rtsp.parse_rtcp(b"\x81\xc9\x00\x07abc").reports == [], "length beyond the data"
    assert rtsp.parse_rtcp(bytes(32)).reports == [], "version 0"
    assert rtsp.parse_rtcp(rtsp.goodbye(5)).bye


def test_sender_report_maps_ntp_to_rtp_time() -> None:
    wall = 1_700_000_000.25
    ntp = rtsp.ntp_timestamp(wall)
    assert ntp >> 32 == 1_700_000_000 + rtsp.NTP_EPOCH_OFFSET
    assert ntp & 0xFFFFFFFF == 1 << 30, "a quarter second"
    packet = rtsp.sender_report(0x1234, ntp, 90_000, packets=10, octets=5000)
    first, kind, words, ssrc, hi, lo, rtp_time, packets, octets = struct.unpack("!BBHIIIIII", packet[:28])
    assert first >> 6 == 2 and kind == rtsp.RTCP_SR and words == 6 and ssrc == 0x1234
    assert (hi << 32 | lo) == ntp and rtp_time == 90_000 and packets == 10 and octets == 5000
    sdes = packet[28:]
    assert sdes[1] == rtsp.RTCP_SDES and len(sdes) % 4 == 0 and rtsp.CNAME in sdes


def test_payloads_are_packetised_once_and_shared() -> None:
    parameters = hevc.parse_hvcc(hvcc())
    frame = media.VideoFrame(timestamp=0, units=[IDR + bytes(5000)], keyframe=True)
    first = rtsp.video_payloads(frame, parameters, videofmt.H265)
    second = rtsp.video_payloads(frame, parameters, videofmt.H265)
    assert first is second
    a = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=1, sequence=10, timestamp_base=5)
    b = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=2, sequence=99)
    pa = list(a.video(frame, parameters, videofmt.H265))
    pb = list(b.video(frame, parameters, videofmt.H265))
    assert [p[12:] for p in pa] == [p[12:] for p in pb], "same payloads"
    assert rtp_fields(pa[0])[1:] == (11, 5, 1) and rtp_fields(pb[0])[1:] == (100, 0, 2)


def test_transport_without_explicit_channels_gets_the_tracks_pair() -> None:
    transport = rtsp.parse_transport("RTP/AVP/TCP;unicast", track=1)
    assert transport is not None and transport.interleaved == (2, 3)
    udp = rtsp.parse_transport("RTP/AVP;unicast;client_port=5000-5003")
    assert udp is not None and udp.udp_rtcp_port == 5003
    assert rtsp.parse_transport("RTP/AVP/TCP;interleaved=300-301") is None


# ----------------------------------------------------------------- over the wire


def test_play_reports_rtp_info_that_matches_the_first_packets() -> None:
    hub = media.Hub()
    stream = live_stream(hub)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            played = player.play()
            assert header(played, "session").startswith(player.session + ";timeout=")
            info = header(played, "rtp-info").split(",")
            assert len(info) == 2
            fields = [dict(p.split("=", 1) for p in part.split(";")) for part in info]
            assert fields[0]["url"].endswith("/video1/trackID=0")
            assert fields[1]["url"].endswith("/video1/trackID=1")
            stream.accept(audio_tag(20))  # before a keyframe: held back
            stream.accept(keyframe_tag(40))
            stream.accept(audio_tag(45))
            video = player.channel(0)
            assert video is not None
            _, seq, ts, _ = rtp_fields(video)
            assert seq == int(fields[0]["seq"])
            assert (ts - int(fields[0]["rtptime"])) & 0xFFFFFFFF == 40 * 90
            audio = player.channel(2)
            assert audio is not None
            _, aseq, ats, _ = rtp_fields(audio)
            assert aseq == int(fields[1]["seq"])
            assert (ats - int(fields[1]["rtptime"])) & 0xFFFFFFFF == 45 * 16
        finally:
            player.close()


def test_new_clients_start_on_a_keyframe_with_parameter_sets_in_band() -> None:
    hub = media.Hub()
    stream = live_stream(hub, with_audio=False)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            player.play(audio=False)
            stream.accept(inter_tag(40))  # mid-GOP: must not be sent
            stream.accept(keyframe_tag(80))
            packets = [player.channel(0) for _ in range(4)]
            assert all(p is not None for p in packets)
            nal_types = [(p[12] >> 1) & 0x3F for p in packets if p is not None]
            assert nal_types == [hevc.NAL_VPS, hevc.NAL_SPS, hevc.NAL_PPS, 19]
        finally:
            player.close()


def test_interleaved_receiver_reports_and_keepalives_on_one_connection() -> None:
    hub = media.Hub()
    stream = live_stream(hub, with_audio=False)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            player.play(audio=False)
            stream.accept(keyframe_tag(40))
            first = player.channel(0)
            assert first is not None
            ssrc = rtp_fields(first)[3]
            # What ffmpeg does: an RR and a keepalive in the same write.
            player.cseq += 1
            player.send_raw(
                rtsp.interleaved_frame(1, receiver_report(42, ssrc, fraction=26, lost=5, jitter=900))
                + request_bytes("GET_PARAMETER", player.base + "/video1", player.cseq, {"Session": player.session})
            )
            reply = player.reply()
            assert "200 OK" in reply and header(reply, "cseq") == str(player.cseq)
            assert header(reply, "session") == player.session
            assert "200 OK" in player.request("OPTIONS", "/video1")

            assert wait_for(lambda: track_stats(server).get("rr_cumulative_lost") == 5)
            assert track_stats(server)["rr_jitter_ms"] == 10.0
            assert track_stats(server)["rr_fraction_lost"] == 26 / 256
            stats = server.client_stats()["video1"][0]
            assert stats["transport"] == "tcp" and stats["playing"] is True
            telemetry = rtsp.telemetry(hub, server)
            assert telemetry["video1"]["rtsp_clients"] == server.client_stats()["video1"]
        finally:
            player.close()


def test_a_wrong_session_is_refused() -> None:
    hub = media.Hub()
    live_stream(hub, with_audio=False)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            player.play(audio=False)
            assert "454" in player.request("GET_PARAMETER", "/video1", {"Session": "NOPE"})
        finally:
            player.close()


def test_sender_reports_flow_per_track(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rtsp, "SR_INTERVAL", 0.05)
    hub = media.Hub()
    stream = live_stream(hub)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            player.play()
            stream.accept(keyframe_tag(40))
            stream.accept(audio_tag(45))
            video = player.channel(0)
            audio = player.channel(2)
            assert video is not None and audio is not None
            for channel, packet in ((1, video), (3, audio)):
                report = player.channel(channel)
                assert report is not None, f"no SR on channel {channel}"
                _, kind, _, ssrc, hi, _lo, rtp_time = struct.unpack("!BBHIIII", report[:20])
                assert kind == rtsp.RTCP_SR and ssrc == rtp_fields(packet)[3]
                assert abs((hi - rtsp.NTP_EPOCH_OFFSET) - time.time()) < 5
                assert 0 <= (rtp_time - rtp_fields(packet)[2]) & 0xFFFFFFFF < 90_000 * 5
        finally:
            player.close()


def test_teardown_sends_bye_before_the_reply() -> None:
    hub = media.Hub()
    stream = live_stream(hub, with_audio=False)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            player.play(audio=False)
            stream.accept(keyframe_tag(40))
            assert player.channel(0) is not None
            assert "200 OK" in player.request("TEARDOWN", "/video1")
            # Everything before the reply is already buffered: SRs, then the BYE.
            reports = [payload for channel, payload in player.media if channel == 1]
            assert reports and rtsp.parse_rtcp(reports[-1]).bye
            assert rtsp.parse_rtcp(reports[-1]).reports == []
            assert wait_for(lambda: not server.client_stats())
        finally:
            player.close()


def test_sequence_and_timestamps_stay_continuous_across_a_camera_reconnect() -> None:
    hub = media.Hub()
    stream = live_stream(hub, with_audio=False)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            player.play(audio=False)
            for ts in (100_000, 100_040, 100_080):
                stream.accept(keyframe_tag(ts) if ts == 100_000 else inter_tag(ts))
            before = [player.channel(0) for _ in range(6)]  # VPS SPS PPS IDR P P
            assert all(p is not None for p in before)
            # The camera drops the push and comes back with its clock restarted.
            stream._last_wall -= 1.5
            stream.mark_reconnect()
            stream.accept(keyframe_tag(0))
            stream.accept(inter_tag(40))
            after = [player.channel(0) for _ in range(5)]
            assert all(p is not None for p in after)
            fields = [rtp_fields(p) for p in before + after if p is not None]
            seqs = [f[1] for f in fields]
            assert all((b - a) & 0xFFFF == 1 for a, b in zip(seqs, seqs[1:])), seqs
            stamps = [f[2] for f in fields]
            steps = [(b - a) & 0xFFFFFFFF for a, b in zip(stamps, stamps[1:])]
            assert all(step < 90_000 * 3 for step in steps), "no backward or huge jumps"
            jump = (fields[6][2] - fields[5][2]) & 0xFFFFFFFF
            assert 90_000 <= jump <= 90_000 * 2, "the gap advances by the wall-clock time"
            assert len({f[3] for f in fields}) == 1, "same SSRC throughout"
        finally:
            player.close()


def test_a_stale_track_is_not_offered_and_playing_sessions_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rtsp, "STALL_TEARDOWN", 0.3)
    hub = media.Hub()
    stream = live_stream(hub, with_audio=False)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            player.play(audio=False)
            stream.accept(keyframe_tag(40))
            assert player.channel(0) is not None
            # The camera stops pushing. The session ends with a BYE.
            bye = None
            deadline = time.time() + 5
            while time.time() < deadline:
                rtcp = player.channel(1, timeout=1)
                if rtcp is not None and rtsp.parse_rtcp(rtcp).bye:
                    bye = rtcp
                    break
            assert bye is not None
            stream.last_frame_at -= media.STALE_AFTER + 1
            late = Player(server.port)
            try:
                assert "503" in late.request("DESCRIBE", "/video1")
            finally:
                late.close()
        finally:
            player.close()


def test_a_slow_client_never_stalls_ingest_or_other_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rtsp, "SEND_TIMEOUT", 1.0)
    hub = media.Hub()
    stream = live_stream(hub, with_audio=False)
    with serving(hub) as server:
        stuck = Player(server.port, rcvbuf=4096)
        healthy = Player(server.port)
        received: list[bytes] = []
        try:
            stuck.play(audio=False)
            healthy.play(audio=False)
            done = threading.Event()

            def drain() -> None:
                while not done.is_set():
                    packet = healthy.channel(0, timeout=0.2)
                    if packet is not None:
                        received.append(packet)

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            # ~18 MB in 300 frames, a keyframe every 10, far beyond any socket
            # buffer; the stuck client never reads a byte of it.
            frames, size = 300, 60_000
            started = time.monotonic()
            for index in range(frames):
                ts = 1000 + index * 33
                tag = keyframe_tag(ts, size) if index % 10 == 0 else inter_tag(ts, size)
                stream.accept(tag)
                time.sleep(0.005)
            ingest_time = time.monotonic() - started
            assert ingest_time < 5.0, f"ingest was held up for {ingest_time:.1f}s"

            def all_frames() -> bool:
                return len({rtp_fields(p)[2] for p in list(received)}) >= frames

            assert wait_for(all_frames, timeout=15), "the healthy client lost frames"
            done.set()
            reader.join(timeout=2)
            seqs = [rtp_fields(p)[1] for p in received]
            assert all((b - a) & 0xFFFF == 1 for a, b in zip(seqs, seqs[1:]))
            # The stuck client was cut off by the send timeout rather than waited on.
            assert wait_for(lambda: len(server.client_stats().get("video1", [])) == 1, timeout=10)
        finally:
            stuck.close()
            healthy.close()


def test_a_slow_client_skips_to_the_next_keyframe() -> None:
    """Drive the subscriber directly: the overflow resumes on a keyframe."""
    hub = media.Hub()
    stream = live_stream(hub, with_audio=False)
    subscriber = stream.subscribe()
    for index in range(media.QUEUE_DEPTH + 5):
        stream.accept(keyframe_tag(index) if index == 0 else inter_tag(index))
    assert subscriber.overflows == 1 and subscriber.waiting_for_keyframe
    stream.accept(keyframe_tag(10_000))
    frame = subscriber.take(timeout=0.1)
    assert isinstance(frame, media.VideoFrame) and frame.keyframe


def test_udp_transport_carries_rtp_and_rtcp_on_a_port_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rtsp, "SR_INTERVAL", 0.05)
    hub = media.Hub()
    stream = live_stream(hub, with_audio=False)
    rtp_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtcp_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp_in.bind(("127.0.0.1", 0))
    rtcp_in.bind(("127.0.0.1", 0))
    rtp_in.settimeout(5)
    rtcp_in.settimeout(5)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            assert "200 OK" in player.request("DESCRIBE", "/video1")
            setup = player.request("SETUP", "/video1/trackID=0", {
                "Transport": f"RTP/AVP;unicast;client_port={rtp_in.getsockname()[1]}-{rtcp_in.getsockname()[1]}"
            })
            assert "200 OK" in setup
            player.session = header(setup, "session").split(";")[0]
            server_ports = header(setup, "transport").split("server_port=")[1].split(";")[0]
            lo, hi = (int(p) for p in server_ports.split("-"))
            assert lo % 2 == 0 and hi == lo + 1
            assert "200 OK" in player.request("PLAY", "/video1")
            stream.accept(keyframe_tag(40))
            packet, _ = rtp_in.recvfrom(2048)
            ssrc = rtp_fields(packet)[3]
            report, source = rtcp_in.recvfrom(2048)
            assert report[1] == rtsp.RTCP_SR and source[1] == hi
            rtcp_in.sendto(receiver_report(9, ssrc, lost=2), ("127.0.0.1", hi))
            assert wait_for(lambda: track_stats(server).get("rr_cumulative_lost") == 2)
        finally:
            player.close()
            rtp_in.close()
            rtcp_in.close()


def test_an_idle_control_connection_times_out_and_keepalives_hold_it_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rtsp, "SESSION_TIMEOUT", 0.4)
    monkeypatch.setattr(rtsp, "SEND_TIMEOUT", 0.1)  # also the idle poll interval
    hub = media.Hub()
    live_stream(hub, with_audio=False)
    with serving(hub) as server:
        player = Player(server.port)
        try:
            assert "200 OK" in player.request("DESCRIBE", "/video1")
            setup = player.request("SETUP", "/video1/trackID=0", {
                "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"
            })
            assert "200 OK" in setup
            player.session = header(setup, "session").split(";")[0]
            # A keepalive every poll interval keeps the session well past its timeout.
            for _ in range(10):
                time.sleep(0.1)
                assert "200 OK" in player.request("GET_PARAMETER", "/video1")
            assert server.client_stats().get("video1"), "kept alive by keepalives"
            # Then silence: the server ends the session and hangs up.
            assert wait_for(lambda: not server.client_stats(), timeout=5)
            deadline = time.time() + 5
            while not player.closed and time.time() < deadline:
                player._fill(deadline)
            assert player.closed, "the server hung up on the idle client"
        finally:
            player.close()
