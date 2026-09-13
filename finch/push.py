"""Pushing video the way the camera does.

When the controller arms a track it hands over a destination; the camera dials it
and writes `extendedFlv` until told to stop. The container is FLV with two
differences that matter, both measured and both handled here:

* the flags byte is `0x07`, not `0x05`
* every tag is followed by its 4-byte previous-size **and a 16-byte trailer**

The trailer is not padding. Read against unifi-cam-proxy's `clock_sync.py`, which
is a working implementation against real Protect, it is:

    00              one zero byte
    01 5F 90        0x015F90 = 90000 for video tags — the clock rate
    00 2B 11        0x002B11 = 11025 for everything else
    00 × 8          padding
    uint32          elapsed seconds × 100000

Every few seconds an `onClockSync` and an `onMpma` script tag go out as well, so
the receiver can tie stream time to wall time.

The video itself is real HEVC read from an Annex B file. Nothing is encoded here:
finch is a camera that has a video, not a video encoder.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Iterator, Protocol

from unifiwire import amf
from unifiwire import flv
from unifiwire import hevc
import identity
from unifiwire import annexb

VIDEO_CLOCK: Final = 90_000
OTHER_CLOCK: Final = 11_025
TRAILER_PADDING: Final = 8
CLOCK_SYNC_INTERVAL_SEC: Final = 5.0
CONNECT_RETRY_SEC: Final = 2.0

log = logging.getLogger("finch.push")


def trailer(is_video: bool, elapsed: float) -> bytes:
    """The 16 bytes that follow every tag's previous-size field."""
    clock = VIDEO_CLOCK if is_video else OTHER_CLOCK
    return (
        b"\x00"
        + clock.to_bytes(3, "big")
        + bytes(TRAILER_PADDING)
        + struct.pack(">I", int(elapsed * 100_000) & 0xFFFFFFFF)
    )


def tag(kind: flv.TagType, timestamp: int, body: bytes, elapsed: float) -> bytes:
    out = bytearray()
    out.append(int(kind))
    out += len(body).to_bytes(3, "big")
    out += (timestamp & 0xFFFFFF).to_bytes(3, "big")
    out.append((timestamp >> 24) & 0xFF)
    out += b"\x00\x00\x00"  # stream id
    out += body
    out += (flv.TAG_HEADER_LEN + len(body)).to_bytes(flv.PREV_SIZE_LEN, "big")
    out += trailer(kind is flv.TagType.VIDEO, elapsed)
    return bytes(out)


def header() -> bytes:
    return flv.SIGNATURE + bytes([0x01, flv.FLAGS_EXTENDED, 0, 0, 0, flv.HEADER_LEN]) + bytes(4)


def metadata(stream_name: str, channel: identity.Channel, audio: bool = False) -> dict[str, object]:
    """Exactly what the real camera announces — nine keys, no more.

    Not ffmpeg's metadata vocabulary: no width, height, framerate or codec ids.
    The receiver learns geometry from the bitstream and identity from `channelId`
    plus the `streamName` the controller assigned.
    """
    return {
        "audioBandwidth": 64_000.0,
        "audioChannels": 1.0,
        "audioFrequency": 16_000.0,
        "channelId": float(channel.channel_id),
        "extendedFormat": True,
        "hasAudio": audio,
        "hasVideo": True,
        "streamId": float(channel.stream_id),
        "streamName": stream_name,
    }


def clock_sync(stream_timestamp: int) -> dict[str, object]:
    return {
        "streamClock": float(stream_timestamp),
        "streamClockBase": 0.0,
        "wallClock": time.time() * 1000,
    }


def mpma(channel: identity.Channel) -> dict[str, object]:
    """Bitrate envelope. The controller uses it to reason about adaptive rates."""
    rate = float(channel.bitrate)
    return {
        "cs": {"cur": rate, "max": rate, "min": rate / 8},
        "m": {"cur": rate, "max": rate, "min": rate / 2},
        "r": 0.0,
        "sp": {"cur": rate, "max": rate, "min": rate / 10},
        "t": rate / 2,
    }


class VideoSource(Protocol):
    fps: int

    def hvcc(self) -> bytes: ...
    def pictures(self) -> Iterator[tuple[list[bytes], bool]]: ...
    def close(self) -> None: ...


@dataclass
class Source:
    """A video to send, read once and replayed for as long as we are streaming."""

    path: Path
    fps: int = 30

    def hvcc(self) -> bytes:
        units = list(annexb.annex_b_units(self.path.read_bytes()))
        sets = {hevc.nal_type(u): u for u in units if hevc.nal_type(u) in (32, 33, 34)}
        vps, sps, pps = sets.get(32, b""), sets.get(33, b""), sets.get(34, b"")
        if not (vps and sps and pps):
            raise ValueError(f"{self.path} has no VPS/SPS/PPS — is it Annex B HEVC?")
        return annexb.build_hvcc(vps, sps, pps)

    def pictures(self) -> Iterator[tuple[list[bytes], bool]]:
        units = [
            u
            for u in annexb.annex_b_units(self.path.read_bytes())
            if (hevc.nal_type(u) or 0) not in (32, 33, 34)
        ]
        yield from annexb.frames(units)

    def close(self) -> None:
        """A finite file source owns no live resources."""


def video_body(units: list[bytes], keyframe: bool) -> bytes:
    payload = b"".join(len(u).to_bytes(4, "big") + u for u in units)
    frame_type = flv.FrameType.KEY if keyframe else flv.FrameType.INTER
    head = bytes([(int(frame_type) << 4) | flv.CODEC_H265, hevc.PACKET_NALU, 0, 0, 0])
    return head + payload


def config_body(record: bytes) -> bytes:
    head = bytes(
        [
            (int(flv.FrameType.SEQUENCE_HEADER) << 4) | flv.CODEC_H265,
            hevc.PACKET_SEQUENCE_HEADER,
            0,
            0,
            0,
        ]
    )
    return head + record


@dataclass
class Pusher:
    """One armed track: dial the destination and write until stopped."""

    host: str
    port: int
    stream_name: str
    channel: identity.Channel
    source: VideoSource
    pace: bool = True
    started_at: float = field(default_factory=time.time)
    stopping: threading.Event = field(default_factory=threading.Event)
    tags_sent: int = 0
    _thread: threading.Thread | None = None
    _sock: socket.socket | None = None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.source.close()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:  # pragma: no cover - already closed
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                self._session()
            except ValueError as exc:
                log.error("%s: invalid video source (%s)", self.stream_name, exc)
                return
            except OSError as exc:
                log.warning("%s: push ended (%s)", self.stream_name, exc)
            if self.stopping.is_set():
                return
            # The camera retries its destination rather than giving up on it.
            self.stopping.wait(CONNECT_RETRY_SEC)

    def _listen(self, sock: socket.socket) -> None:
        """Whatever the far end says back. It should say nothing; if it does, we want it."""
        try:
            while not self.stopping.is_set():
                sock.settimeout(1.0)
                try:
                    chunk = sock.recv(4096)
                except (TimeoutError, OSError):
                    continue
                if not chunk:
                    log.info("%s: receiver closed the connection", self.stream_name)
                    return
                log.info("%s: receiver sent %d bytes: %s", self.stream_name, len(chunk),
                         chunk[:64].hex(" "))
        except OSError:  # pragma: no cover - socket torn down under us
            return

    def _session(self) -> None:
        log.info("%s: pushing to %s:%d", self.stream_name, self.host, self.port)
        sock = socket.create_connection((self.host, self.port), timeout=10)
        self._sock = sock
        threading.Thread(target=self._listen, args=(sock,), daemon=True).start()
        try:
            sock.sendall(header())
            self._send(sock, flv.TagType.SCRIPT, 0,
                       amf.script_body("onMetaData", metadata(self.stream_name, self.channel)))
            self._send(sock, flv.TagType.VIDEO, 0, config_body(self.source.hvcc()))

            step = max(1, round(1000 / max(1, self.source.fps)))
            interval = 1.0 / max(1, self.source.fps) if self.pace else 0.0
            timestamp = 0
            last_sync = 0.0
            while not self.stopping.is_set():
                for units, keyframe in self.source.pictures():
                    if self.stopping.is_set():
                        return
                    if self.elapsed - last_sync >= CLOCK_SYNC_INTERVAL_SEC:
                        last_sync = self.elapsed
                        self._send(sock, flv.TagType.SCRIPT, timestamp,
                                   amf.script_body("onClockSync", clock_sync(timestamp)))
                        self._send(sock, flv.TagType.SCRIPT, timestamp,
                                   amf.script_body("onMpma", mpma(self.channel)))
                    self._send(sock, flv.TagType.VIDEO, timestamp, video_body(units, keyframe))
                    timestamp += step
                    if interval:
                        time.sleep(interval)
        finally:
            self._sock = None
            self.source.close()
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass

    def _send(self, sock: socket.socket, kind: flv.TagType, timestamp: int, body: bytes) -> None:
        sock.sendall(tag(kind, timestamp, body, self.elapsed))
        self.tags_sent += 1
