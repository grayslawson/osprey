"""Media ingest — the camera connects to us and pushes.

Once a track is armed with a destination, the camera dials that address and writes
`extendedFlv` at it, reconnecting on its own if the socket drops. So this is a
plain TCP listener, one thread per connection.

Tracks are told apart by the `streamName` in the metadata tag and never by port,
because the camera may collapse several tracks onto one connection.

What arrives is republished to subscribers (RTSP, snapshots, anything else) as
decodable frames rather than raw tags, with the parameter sets held aside so a
subscriber that joins mid-stream can still start.
"""

from __future__ import annotations

import logging
import socket
import socketserver
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Final, Iterator

from unifiwire import flv
from unifiwire import hevc
import videofmt

INGEST_PORT: Final = 7550
MJPEG_PORT: Final = 7551
READ_SIZE: Final = 65536
QUEUE_DEPTH: Final = 120  # a slow subscriber loses frames rather than stalling ingest
QUEUE_BYTES: Final = 8 * 1024 * 1024  # and never buffers more than this per subscriber
STALE_AFTER: Final = 5.0  # seconds without a frame before a track stops counting as live
# A timestamp step this far from what the wall clock says is a new timeline (the
# camera reconnected or its clock wrapped), not media time passing.
BACKWARD_TOLERANCE_MS: Final = 1000
FORWARD_TOLERANCE_MS: Final = 5000
RECONNECT_TOLERANCE_MS: Final = 2000
BANDWIDTH_WINDOW: Final = 60.0  # seconds of byte samples kept for the live rate/graph
BANDWIDTH_BUCKETS: Final = 40  # time slices the window is drawn as (the sparkline width)

log = logging.getLogger("cuckoo.media")


@dataclass(frozen=True)
class VideoFrame:
    timestamp: int
    units: list[bytes]
    keyframe: bool
    # When the frame reached us (monotonic). RTCP sender reports map RTP time to
    # wall-clock time through it, so it is the arrival, not the send, that counts.
    arrival: float = field(default=0.0, compare=False)
    # Shared, per-frame scratch space: RTSP packetises a frame once and every
    # client reuses the payloads. Not part of the frame's value.
    cache: dict[object, object] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class AudioFrame:
    timestamp: int
    payload: bytes
    arrival: float = field(default=0.0, compare=False)


Frame = VideoFrame | AudioFrame


def frame_size(frame: Frame) -> int:
    if isinstance(frame, VideoFrame):
        return sum(len(unit) for unit in frame.units)
    return len(frame.payload)


class Subscriber:
    """A bounded view of one stream. Falls behind by dropping, never by blocking.

    The bound is both a frame count and a byte budget. When either is exceeded the
    whole backlog is discarded and the subscriber resumes at the next keyframe:
    dropping frames one at a time from the head of a GOP would hand the decoder
    P-frames whose reference picture is gone, which is worse than a short gap.
    A subscriber made with `wait_for_keyframe` also *starts* on a keyframe.
    """

    def __init__(
        self,
        depth: int = QUEUE_DEPTH,
        max_bytes: int = QUEUE_BYTES,
        wait_for_keyframe: bool = False,
    ) -> None:
        self._frames: deque[Frame] = deque()
        self._bytes = 0
        self._depth = depth
        self._max_bytes = max_bytes
        self._cond = threading.Condition()
        self._resync = wait_for_keyframe
        self.dropped = 0  # frames discarded on overflow or while resynchronising
        self.overflows = 0  # times the backlog was discarded

    @property
    def waiting_for_keyframe(self) -> bool:
        with self._cond:
            return self._resync

    @property
    def backlog(self) -> int:
        with self._cond:
            return len(self._frames)

    def offer(self, frame: Frame) -> None:
        keyframe = isinstance(frame, VideoFrame) and frame.keyframe
        size = frame_size(frame)
        with self._cond:
            if self._resync and not keyframe:
                self.dropped += 1
                return
            if self._frames and (
                len(self._frames) >= self._depth or self._bytes + size > self._max_bytes
            ):
                self.dropped += len(self._frames)
                self.overflows += 1
                self._frames.clear()
                self._bytes = 0
                if not keyframe:
                    self._resync = True
                    self.dropped += 1
                    return
            self._resync = False
            self._frames.append(frame)
            self._bytes += size
            self._cond.notify()

    def take(self, timeout: float = 1.0) -> Frame | None:
        with self._cond:
            if not self._frames:
                self._cond.wait(timeout)
            if not self._frames:
                return None
            frame = self._frames.popleft()
            self._bytes -= frame_size(frame)
            return frame


@dataclass
class Stream:
    """Live state for one track: what a late subscriber needs, plus counters."""

    name: str
    codec_id: int | None = None
    fmt: videofmt.VideoFormat = videofmt.H265
    parameters: videofmt.VideoParameters = field(default_factory=hevc.ParameterSets)
    audio_specific_config: bytes = b""
    frames: int = 0
    keyframes: int = 0
    bytes_in: int = 0
    last_timestamp: int = 0
    last_frame_at: float = 0.0  # monotonic arrival of the newest frame; 0 before any
    rebases: int = 0  # timeline discontinuities absorbed (camera reconnects, wraps)
    _ts_offset: int = 0
    _last_raw: int | None = None
    _last_out: int = 0
    _last_wall: float = 0.0
    _reconnected: bool = False
    _subscribers: list[Subscriber] = field(default_factory=list)
    _samples: deque[tuple[float, int]] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def ready(self) -> bool:
        """True once a subscriber joining now could start decoding."""
        return self.parameters.complete

    def frame_age(self, now: float | None = None) -> float | None:
        """Seconds since the newest frame arrived, or None if none ever has."""
        if not self.last_frame_at:
            return None
        return max(0.0, (time.monotonic() if now is None else now) - self.last_frame_at)

    @property
    def live(self) -> bool:
        """Playable *and* the camera is pushing right now.

        `ready` alone stays true forever once parameter sets were seen, so a
        client that DESCRIBEs a track the camera stopped pushing would be handed
        a session that never carries a byte.
        """
        age = self.frame_age()
        return self.ready and age is not None and age < STALE_AFTER

    @property
    def position(self) -> int:
        """The newest frame's timestamp on the continuous output timeline (ms)."""
        return self._last_out

    def mark_reconnect(self) -> None:
        """A new ingest connection started feeding this track."""
        self._reconnected = True

    def _timeline(self, raw: int, now: float) -> int:
        """Map the camera's tag timestamp onto one continuous timeline.

        Across a camera reconnect the FLV clock usually restarts. Subscribers keep
        their RTP sessions, so the output timeline is rebased to carry on from
        where it was, advanced by the wall-clock time the gap took. Video and audio
        share the FLV clock, so one offset keeps them in sync.
        """
        reconnected, self._reconnected = self._reconnected, False
        if self._last_raw is not None:
            delta = raw - self._last_raw
            elapsed = int((now - self._last_wall) * 1000)
            jumped = delta < -BACKWARD_TOLERANCE_MS or delta > elapsed + FORWARD_TOLERANCE_MS
            if reconnected and abs(delta - elapsed) > RECONNECT_TOLERANCE_MS:
                jumped = True
            if jumped:
                self._ts_offset = self._last_out + max(1, elapsed) - raw
                self.rebases += 1
                log.info("%s: timeline rebased (camera clock stepped %+d ms in %d ms)",
                         self.name, delta, elapsed)
        out = raw + self._ts_offset
        self._last_raw = raw
        self._last_out = out
        self._last_wall = now
        return out

    @property
    def audio(self) -> tuple[int, int, int] | None:
        if not self.audio_specific_config:
            return None
        return hevc.audio_config(self.audio_specific_config)

    def subscribe(self, wait_for_keyframe: bool = True) -> Subscriber:
        subscriber = Subscriber(wait_for_keyframe=wait_for_keyframe)
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    @property
    def dropped_total(self) -> int:
        with self._lock:
            return sum(s.dropped for s in self._subscribers)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def bandwidth(
        self, window: float = BANDWIDTH_WINDOW, buckets: int = BANDWIDTH_BUCKETS
    ) -> tuple[float, list[float]]:
        """Windowed ingest telemetry for this track: (bytes/sec, per-bucket series).

        The series buckets the window's byte samples into equal time slices, each
        reported as bytes/sec, so the status page can draw a live sparkline rather
        than one lifetime total. Empty slices read 0.
        """
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            samples = list(self._samples)
        rate = sum(n for _, n in samples) / window
        slice_s = window / buckets
        start = now - window
        series = [0.0] * buckets
        for ts, n in samples:
            idx = int((ts - start) / slice_s)
            series[min(buckets - 1, max(0, idx))] += n
        return rate, [v / slice_s for v in series]

    def _fan_out(self, frame: Frame) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for target in targets:
            target.offer(frame)

    def accept(self, tag: flv.Tag) -> Frame | None:
        """Turn one tag into a frame and republish it. Config tags return None."""
        self.bytes_in += len(tag.body)
        self.last_timestamp = tag.timestamp
        if tag.body:
            now = time.monotonic()
            with self._lock:
                self._samples.append((now, len(tag.body)))
                cutoff = now - BANDWIDTH_WINDOW
                while self._samples and self._samples[0][0] < cutoff:
                    self._samples.popleft()
        if tag.kind is flv.TagType.VIDEO:
            return self._accept_video(tag)
        if tag.kind is flv.TagType.AUDIO:
            return self._accept_audio(tag)
        return None

    def _accept_video(self, tag: flv.Tag) -> Frame | None:
        self.codec_id = tag.codec_id
        if tag.codec_id is not None:
            self.fmt = videofmt.for_codec_id(tag.codec_id)
        packet = hevc.video_packet(tag.body)
        # Config arrives two different ways on this wire. HEVC marks it with FLV
        # frame type 6 and sets the AVCPacketType byte to 1 even on config; H.264
        # uses the standard convention — frame type 1 (a keyframe) with the
        # AVCPacketType byte 0 and an `avcC` record. Accept either so both codecs'
        # parameter sets are found.
        if tag.is_sequence_header or (packet is not None and packet.is_config):
            parameters = self.fmt.parse(tag.body)
            if parameters.complete:
                self.parameters = parameters
                log.info("%s: %s parameter sets received, stream is playable",
                         self.name, self.fmt.rtpmap)
            return None
        if packet is None:
            return None
        units = list(hevc.split_nalus(packet.payload, self.parameters.length_size))
        if not units:
            return None
        if not self.parameters.complete:
            # Some cameras also repeat the sets in band ahead of a keyframe rather
            # than in a config tag; recover them from the units when they do.
            recovered = self.fmt.collect(units, self.parameters.length_size)
            if recovered.complete:
                self.parameters = recovered
                log.info("%s: %s parameter sets recovered in band, stream is playable",
                         self.name, self.fmt.rtpmap)
        now = time.monotonic()
        frame = VideoFrame(
            timestamp=self._timeline(tag.timestamp, now),
            units=units,
            keyframe=tag.is_keyframe or any(self.fmt.is_keyframe(u) for u in units),
            arrival=now,
        )
        self.last_frame_at = now
        self.frames += 1
        if frame.keyframe:
            self.keyframes += 1
        self._fan_out(frame)
        return frame

    def _accept_audio(self, tag: flv.Tag) -> Frame | None:
        packet = hevc.audio_packet(tag.body)
        if packet is None or not packet.is_aac:
            return None
        if packet.is_config:
            self.audio_specific_config = packet.payload
            log.info("%s: audio config %s", self.name, self.audio)
            return None
        if not packet.payload:
            return None
        now = time.monotonic()
        frame = AudioFrame(
            timestamp=self._timeline(tag.timestamp, now), payload=packet.payload, arrival=now
        )
        self.last_frame_at = now
        self._fan_out(frame)
        return frame


class Hub:
    """Every live stream, keyed by the name we assigned the track."""

    def __init__(self) -> None:
        self.streams: dict[str, Stream] = {}
        self._lock = threading.Lock()

    def stream(self, name: str) -> Stream:
        with self._lock:
            return self.streams.setdefault(name, Stream(name=name))

    def get(self, name: str) -> Stream | None:
        with self._lock:
            return self.streams.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self.streams)

    def stats(self, window: float = BANDWIDTH_WINDOW) -> dict[str, dict[str, object]]:
        """Per-track runtime telemetry, keyed by track name: byte-rate and its
        windowed series (for a sparkline), lifetime bytes, frame/keyframe counts,
        live RTSP subscribers, and whether the track is playable yet."""
        with self._lock:
            streams = list(self.streams.items())
        out: dict[str, dict[str, object]] = {}
        for name, stream in streams:
            rate, series = stream.bandwidth(window)
            out[name] = {
                "rate_bps": rate,
                "series": series,
                "bytes_in": stream.bytes_in,
                "frames": stream.frames,
                "keyframes": stream.keyframes,
                "subscribers": stream.subscriber_count,
                "playable": stream.ready,
                "live": stream.live,
                "last_frame_age": stream.frame_age(),
                "rebases": stream.rebases,
                "dropped": stream.dropped_total,
            }
        return out

    def publish(self, name: str, tag: flv.Tag) -> Frame | None:
        return self.stream(name).accept(tag)


class Connection:
    """One camera connection: deframe, name the stream, hand tags to the hub.

    Split out from the socket so it can be driven byte-for-byte by a test.
    """

    def __init__(self, hub: Hub, fallback_name: str) -> None:
        self.hub = hub
        self.name = fallback_name
        self.named = False
        self._seen: set[str] = set()
        self._deframer = flv.Deframer()

    def feed(self, chunk: bytes) -> Iterator[Frame]:
        for tag in self._deframer.feed(chunk):
            if tag.kind is flv.TagType.SCRIPT:
                found = flv.stream_name(tag.body)
                if found:
                    self.name = found
                    self.named = True
                    log.info("stream named %s", found)
                continue
            if self.name not in self._seen:
                self._seen.add(self.name)
                self.hub.stream(self.name).mark_reconnect()
            frame = self.hub.publish(self.name, tag)
            if frame is not None:
                yield frame


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, IngestServer)
        peer = self.client_address[0]
        if server.allowed_peers and peer not in server.allowed_peers:
            log.warning("rejecting media ingest from unregistered peer %s", peer)
            return
        connection = Connection(server.hub, fallback_name=f"{server.fallback_name}")
        log.info("ingest connection from %s", peer)
        sock = self.request
        assert isinstance(sock, socket.socket)
        try:
            while not server.closing.is_set():
                chunk = sock.recv(READ_SIZE)
                if not chunk:
                    break
                for _ in connection.feed(chunk):
                    pass
        except OSError as exc:
            log.debug("ingest connection from %s ended: %s", peer, exc)
        finally:
            stream = server.hub.get(connection.name)
            log.info(
                "ingest connection from %s closed (%s, %d frames)",
                peer, connection.name, stream.frames if stream else 0,
            )


class IngestServer(socketserver.ThreadingTCPServer):
    """Threaded listener for pushed media."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        hub: Hub,
        port: int = INGEST_PORT,
        fallback_name: str = "video1",
        allowed_peers: set[str] | frozenset[str] | None = None,
        bind_host: str = "0.0.0.0",
    ) -> None:
        self.hub = hub
        self.fallback_name = fallback_name
        self.allowed_peers = frozenset(allowed_peers or ())
        self.closing = threading.Event()
        super().__init__((bind_host, port), _Handler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        log.info("media ingest listening on :%d", self.server_address[1])
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.closing.set()
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
