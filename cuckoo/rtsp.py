"""RTSP server — the north-side media face.

What the camera pushes at us is republished here as ordinary RTSP/RTP, so any
client can play it: `ffplay rtsp://<us>:8554/video1`, VLC, or an ONVIF client
following the URI in a media profile.

Two things about this stream are not what a receiver would assume, and both are
handled here rather than left to the client:

* the video is **HEVC or H.264** — whichever the track carries — packetised per
  RFC 7798 / RFC 6184 (single NAL, or FU fragments). The codec is chosen per stream
  by its `VideoFormat` (see `videofmt`), so nothing here branches on it directly.
* the audio is **AAC-LC at 16 kHz mono**, so the RTP clock rate is 16000

Parameter sets go out in the SDP *and* ahead of every keyframe, so a client that
joins mid-stream can start without waiting for the next configuration record.

Transport: TCP interleaved and UDP unicast. Interleaved is the reliable choice on
a busy network and what `-rtsp_transport tcp` selects.

Session behaviour several clients (go2rtc, ffmpeg, UniFi Protect) share one
stream with, so each piece is deliberate:

* the control connection is demultiplexed as bytes: `$`-framed RTCP receiver
  reports from the client are consumed by length and recorded, never decoded
  as text, so they can neither corrupt a keepalive nor grow a buffer;
* every track sends RTCP sender reports (NTP <-> RTP mapping) every couple of
  seconds and a BYE on teardown, so a client's read deadline is refreshed even
  across a short camera reconnect;
* PLAY answers with RTP-Info; a new client starts on a keyframe, with the
  parameter sets in band, and its sequence/timestamps stay continuous because
  the stream rebases its timeline when the camera's clock restarts;
* a slow client only ever hurts itself: its bounded queue skips to the next
  keyframe, and a write blocked for `SEND_TIMEOUT` drops it;
* a track the camera is not pushing is not offered (503), and playing sessions
  end with a BYE once their track has been silent for `STALL_TEARDOWN`.
"""

from __future__ import annotations

import base64
import logging
import random
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Final, Iterator, cast

import media
import videofmt
from principals import ROLE_MEDIA, DigestVerifier, Network, Principal, peer_in

RTSP_PORT: Final = 8554
VIDEO_PAYLOAD_TYPE: Final = 96
AUDIO_PAYLOAD_TYPE: Final = 97
VIDEO_CLOCK: Final = 90_000
MTU_PAYLOAD: Final = 1400

VIDEO_TRACK: Final = 0
AUDIO_TRACK: Final = 1

log = logging.getLogger("cuckoo.rtsp")


class RtspAuth:
    """Optional RTSP Digest authentication (RFC 7616), one or more principals.

    ``RtspAuth(username, password)`` keeps the original single-credential form.
    ``RtspAuth(users=...)`` accepts every principal holding the ``media`` (or
    ``admin``) role, each optionally limited to source CIDRs. ``anonymous_from``
    admits listed hosts without credentials while everyone else must
    authenticate.
    """

    REALM: Final = "Osprey RTSP"
    MAX_NONCES: Final[int] = DigestVerifier.MAX_NONCES

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        nonce_ttl: int = 300,
        *,
        users: tuple[Principal, ...] = (),
        anonymous_from: tuple[Network, ...] = (),
    ) -> None:
        principals = list(users)
        if username:
            if not password:
                raise ValueError("RTSP username requires a password")
            if any(user.username == username for user in principals):
                raise ValueError(f"RTSP user {username!r} is configured twice")
            principals.append(Principal(username, password, frozenset({ROLE_MEDIA})))
        self.users = tuple(p for p in principals if p.has(ROLE_MEDIA))
        self.anonymous_from = anonymous_from
        self._verifier = DigestVerifier(self.REALM, self._lookup, nonce_ttl)

    def _lookup(self, username: str) -> Principal | None:
        return next((user for user in self.users if user.username == username), None)

    def challenge(self) -> str:
        return self._verifier.challenge()

    def authenticate(self, header: str, method: str, uri: str, peer: str = "") -> Principal | None:
        principal = self._verifier.authenticate(header, method, uri)
        if principal is None or (peer and not principal.permits_peer(peer)):
            return None
        if not peer and principal.allow_from:
            return None
        return principal

    def valid(self, header: str, method: str, uri: str, peer: str = "") -> bool:
        if peer and self.anonymous_from and peer_in(peer, self.anonymous_from):
            return True
        return self.authenticate(header, method, uri, peer) is not None


def sdp(host: str, name: str, stream: media.Stream) -> str:
    """Describe the stream, including the parameter sets a decoder needs first."""
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {host}",
        f"s={name}",
        "c=IN IP4 0.0.0.0",
        "t=0 0",
        "a=control:*",
        f"m=video 0 RTP/AVP {VIDEO_PAYLOAD_TYPE}",
        f"a=rtpmap:{VIDEO_PAYLOAD_TYPE} {stream.fmt.rtpmap}/{VIDEO_CLOCK}",
        stream.fmt.fmtp(VIDEO_PAYLOAD_TYPE, stream.parameters),
        f"a=control:trackID={VIDEO_TRACK}",
    ]
    audio = stream.audio
    if audio is not None:
        _, rate, channels = audio
        config = stream.audio_specific_config.hex()
        lines += [
            f"m=audio 0 RTP/AVP {AUDIO_PAYLOAD_TYPE}",
            f"a=rtpmap:{AUDIO_PAYLOAD_TYPE} mpeg4-generic/{rate}/{max(1, channels)}",
            f"a=fmtp:{AUDIO_PAYLOAD_TYPE} streamtype=5;profile-level-id=1;mode=AAC-hbr;"
            f"config={config};sizelength=13;indexlength=3;indexdeltalength=3",
            f"a=control:trackID={AUDIO_TRACK}",
        ]
    return "\r\n".join(lines) + "\r\n"


def rtp_header(
    payload_type: int, sequence: int, timestamp: int, ssrc: int, marker: bool
) -> bytes:
    first = 0x80  # version 2, no padding, no extension, no CSRCs
    second = (0x80 if marker else 0) | payload_type
    return struct.pack("!BBHII", first, second, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc)


# ------------------------------------------------------------- shared payloads

Payloads = tuple[tuple[bytes, bool], ...]  # (RTP payload, marker) per packet


def _fragments(unit: bytes, fmt: videofmt.VideoFormat, final_unit: bool) -> Iterator[tuple[bytes, bool]]:
    """A fragmentation unit: the codec's payload header, then S/E/type per fragment.

    RFC 7798 (HEVC) uses a two-byte FU header; RFC 6184 (H.264) a one-byte one.
    Both then carry a start/end/type byte on every fragment and the NAL body
    with its header stripped.
    """
    if fmt.nal_header_len == 2:  # HEVC — RFC 7798
        kind = (unit[0] >> 1) & 0x3F
        header = bytes([(fmt.fu_type << 1) | (unit[0] & 0x81), unit[1]])
    else:  # H.264 — RFC 6184 FU-A: keep F and NRI, replace type with 28
        kind = unit[0] & 0x1F
        header = bytes([(unit[0] & 0xE0) | fmt.fu_type])
    body = unit[fmt.nal_header_len :]
    chunk = MTU_PAYLOAD - (len(header) + 1)
    for index, at in enumerate(range(0, len(body), chunk)):
        start = index == 0
        end = at + chunk >= len(body)
        fu = bytes([(0x80 if start else 0) | (0x40 if end else 0) | kind])
        yield header + fu + body[at : at + chunk], end and final_unit


def video_payloads(
    frame: media.VideoFrame, parameters: videofmt.VideoParameters, fmt: videofmt.VideoFormat
) -> Payloads:
    """The RTP payloads for one frame, computed once and shared by every client.

    Only the 12-byte RTP header differs between clients (sequence, timestamp
    base, SSRC), so the NAL splitting and fragmentation is cached on the frame.
    Keyframes carry the parameter sets in band, so a client that joined
    mid-stream — or whose decoder lost state — can start without the SDP.
    """
    prefix = tuple(parameters.sets) if frame.keyframe and parameters.complete else ()
    key = ("rtp", fmt.rtpmap, prefix)
    cached = frame.cache.get(key)
    if cached is not None:
        return cast(Payloads, cached)
    units = list(prefix) + list(frame.units)
    out: list[tuple[bytes, bool]] = []
    for index, unit in enumerate(units):
        last = index == len(units) - 1
        if len(unit) <= MTU_PAYLOAD:
            out.append((unit, last))
        elif len(unit) > fmt.nal_header_len:
            out.extend(_fragments(unit, fmt, final_unit=last))
    payloads = tuple(out)
    frame.cache[key] = payloads
    return payloads


@dataclass
class Packetiser:
    """Turns frames into RTP packets. One instance per track per client.

    The sequence number and the timestamp base are per client (and random in
    production, RFC 3550 §5.1); the media timeline underneath comes from the
    stream, which already keeps it continuous across camera reconnects.
    """

    payload_type: int
    clock: int
    ssrc: int
    sequence: int = 0
    timestamp_base: int = 0
    packets: int = 0
    octets: int = 0
    last_rtp: int | None = None
    last_arrival: float = 0.0

    @property
    def next_sequence(self) -> int:
        return (self.sequence + 1) & 0xFFFF

    def rtp_time(self, timestamp_ms: int) -> int:
        return (self.timestamp_base + timestamp_ms * self.clock // 1000) & 0xFFFFFFFF

    def rtp_now(self, now: float) -> int | None:
        """The RTP time corresponding to monotonic `now`, for a sender report."""
        if self.last_rtp is None:
            return None
        return (self.last_rtp + int((now - self.last_arrival) * self.clock)) & 0xFFFFFFFF

    def _next(self, timestamp_ms: int, payload: bytes, marker: bool, arrival: float) -> bytes:
        self.sequence = (self.sequence + 1) & 0xFFFF
        timestamp = self.rtp_time(timestamp_ms)
        self.packets += 1
        self.octets += len(payload)
        self.last_rtp = timestamp
        self.last_arrival = arrival
        return rtp_header(self.payload_type, self.sequence, timestamp, self.ssrc, marker) + payload

    def video(
        self,
        frame: media.VideoFrame,
        parameters: videofmt.VideoParameters,
        fmt: videofmt.VideoFormat,
    ) -> Iterator[bytes]:
        arrival = frame.arrival or time.monotonic()
        for payload, marker in video_payloads(frame, parameters, fmt):
            yield self._next(frame.timestamp, payload, marker, arrival)

    def audio(self, frame: media.AudioFrame) -> Iterator[bytes]:
        """AAC-hbr: a two-byte AU header length, then 13 bits of size, 3 of index."""
        size = len(frame.payload) & 0x1FFF
        au_header = struct.pack("!HH", 16, size << 3)
        arrival = frame.arrival or time.monotonic()
        yield self._next(frame.timestamp, au_header + frame.payload, True, arrival)


# ------------------------------------------------------------------------ RTCP

RTCP_SR: Final = 200
RTCP_RR: Final = 201
RTCP_SDES: Final = 202
RTCP_BYE: Final = 203
NTP_EPOCH_OFFSET: Final = 2_208_988_800  # seconds from 1900 to 1970
CNAME: Final = b"osprey"


def ntp_timestamp(wall: float) -> int:
    """A 64-bit NTP timestamp (RFC 3550 §4) for a Unix wall-clock time."""
    seconds = int(wall)
    fraction = int((wall - seconds) * (1 << 32)) & 0xFFFFFFFF
    return (((seconds + NTP_EPOCH_OFFSET) & 0xFFFFFFFF) << 32) | fraction


def _sdes(ssrc: int) -> bytes:
    item = bytes([1, len(CNAME)]) + CNAME  # CNAME
    chunk = struct.pack("!I", ssrc) + item + b"\x00"
    chunk += bytes(-len(chunk) % 4)
    return struct.pack("!BBH", 0x81, RTCP_SDES, len(chunk) // 4) + chunk


def sender_report(ssrc: int, ntp: int, rtp_time: int, packets: int, octets: int) -> bytes:
    """A compound RTCP packet: SR with no report blocks, then SDES CNAME."""
    sr = struct.pack(
        "!BBHIIIIII", 0x80, RTCP_SR, 6, ssrc, ntp >> 32, ntp & 0xFFFFFFFF,
        rtp_time & 0xFFFFFFFF, packets & 0xFFFFFFFF, octets & 0xFFFFFFFF,
    )
    return sr + _sdes(ssrc)


def goodbye(ssrc: int) -> bytes:
    return struct.pack("!BBHI", 0x81, RTCP_BYE, 1, ssrc)


@dataclass(frozen=True)
class ReceptionReport:
    """One report block from a receiver (RFC 3550 §6.4.1) about one of our SSRCs."""

    ssrc: int
    fraction_lost: float  # 0..1 since the previous report
    cumulative_lost: int
    highest_sequence: int
    jitter: int  # in RTP clock units


@dataclass
class RtcpSummary:
    reports: list[ReceptionReport] = field(default_factory=list)
    bye: bool = False


def parse_rtcp(data: bytes) -> RtcpSummary:
    """Read a compound RTCP packet from a client. Malformed input stops parsing."""
    summary = RtcpSummary()
    at = 0
    while at + 4 <= len(data):
        first, kind, words = struct.unpack_from("!BBH", data, at)
        size = (words + 1) * 4
        if first >> 6 != 2 or at + size > len(data):
            break
        count = first & 0x1F
        if kind in (RTCP_SR, RTCP_RR):
            blocks = at + 8 + (20 if kind == RTCP_SR else 0)
            for index in range(count):
                block = blocks + index * 24
                if block + 24 > at + size:
                    break
                ssrc, lost_word, highest, jitter = struct.unpack_from("!IIII", data, block)
                cumulative = lost_word & 0xFFFFFF
                if cumulative & 0x800000:
                    cumulative -= 1 << 24
                summary.reports.append(ReceptionReport(
                    ssrc=ssrc, fraction_lost=(lost_word >> 24) / 256,
                    cumulative_lost=cumulative, highest_sequence=highest, jitter=jitter,
                ))
        elif kind == RTCP_BYE:
            summary.bye = True
        at += size
    return summary


# ------------------------------------------------------------------- transport


@dataclass
class Transport:
    """How one track's packets reach this client."""

    interleaved: tuple[int, int] | None = None
    udp_host: str = ""
    udp_port: int = 0
    udp_rtcp_port: int = 0

    @property
    def is_tcp(self) -> bool:
        return self.interleaved is not None

    def describe(self, track_udp_port: int = 0) -> str:
        if self.interleaved is not None:
            return f"RTP/AVP/TCP;unicast;interleaved={self.interleaved[0]}-{self.interleaved[1]}"
        return (
            f"RTP/AVP;unicast;client_port={self.udp_port}-{self.udp_rtcp_port or self.udp_port + 1};"
            f"server_port={track_udp_port}-{track_udp_port + 1}"
        )


def parse_transport(header: str, track: int = VIDEO_TRACK) -> Transport | None:
    """Read the client's Transport header. Interleaved wins if both are offered.

    Only the first transport spec is considered. `RTP/AVP/TCP` without an
    explicit `interleaved=` gets the conventional channels for the track.
    """
    spec = header.split(",", 1)[0]
    parts = [p.strip() for p in spec.split(";")]
    transport = Transport()
    for part in parts:
        if part.startswith("interleaved="):
            lo, _, hi = part[len("interleaved=") :].partition("-")
            try:
                low = int(lo)
                high = int(hi) if hi else low + 1
            except ValueError:
                return None
            if not (0 <= low <= 255 and 0 <= high <= 255):
                return None
            transport.interleaved = (low, high)
        elif part.startswith("client_port="):
            lo, _, hi = part[len("client_port=") :].partition("-")
            try:
                transport.udp_port = int(lo)
                transport.udp_rtcp_port = int(hi) if hi else transport.udp_port + 1
            except ValueError:
                return None
            if not (0 < transport.udp_port < 65536 and 0 < transport.udp_rtcp_port < 65536):
                return None
    if transport.interleaved is None and parts and parts[0].upper().endswith("/TCP"):
        transport.interleaved = (2 * track, 2 * track + 1)
    if transport.interleaved is None and transport.udp_port == 0:
        return None
    return transport


# --------------------------------------------------------------------- requests


@dataclass
class Request:
    method: str
    uri: str
    headers: dict[str, str]
    body: bytes = b""

    @property
    def cseq(self) -> str:
        return self.headers.get("cseq", "0")

    @property
    def session(self) -> str:
        return self.headers.get("session", "").split(";", 1)[0].strip()

    @property
    def path(self) -> str:
        after_scheme = self.uri.split("://", 1)[-1]
        path = after_scheme.partition("/")[2]
        return "/" + path

    @property
    def stream_name(self) -> str:
        return self.path.lstrip("/").split("/")[0].split("?")[0]

    @property
    def track(self) -> int | None:
        marker = "trackID="
        at = self.path.find(marker)
        if at < 0:
            return None
        try:
            return int(self.path[at + len(marker) :].split("/")[0])
        except ValueError:
            return None


def parse_request(text: str) -> Request | None:
    lines = text.split("\r\n")
    if not lines or len(lines[0].split()) < 2:
        return None
    method, uri = lines[0].split()[0].upper(), lines[0].split()[1]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return Request(method=method, uri=uri, headers=headers)


@dataclass(frozen=True)
class Interleaved:
    """A `$`-framed binary packet on the control connection (RFC 2326 §10.12)."""

    channel: int
    payload: bytes


class ProtocolError(ValueError):
    """The control stream cannot be parsed any further; the connection must end."""


MAX_HEADER: Final = 16 * 1024
MAX_BODY: Final = 64 * 1024


class ControlReader:
    """Demultiplexes an RTSP control connection: text requests and `$` frames.

    Clients such as ffmpeg send RTCP receiver reports interleaved on the same
    TCP connection as their keepalive requests. Those are binary and must be
    consumed by length, never decoded as text: decoding them corrupts the next
    request and lets the buffer grow without bound.
    """

    def __init__(self, max_header: int = MAX_HEADER, max_body: int = MAX_BODY) -> None:
        self._buffer = bytearray()
        self._max_header = max_header
        self._max_body = max_body
        self.skipped = 0  # stray bytes discarded between messages

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[Request | Interleaved]:
        self._buffer += data
        out: list[Request | Interleaved] = []
        while True:
            item = self._next()
            if item is None:
                return out
            out.append(item)

    def _resync(self) -> None:
        """Drop bytes that cannot start a message (stray CRLF, garbage)."""
        buffer = self._buffer
        index = 0
        while index < len(buffer) and buffer[index] != 0x24 and not 0x41 <= buffer[index] <= 0x5A:
            index += 1
        if index:
            self.skipped += index
            del buffer[:index]

    def _next(self) -> Request | Interleaved | None:
        buffer = self._buffer
        while True:
            self._resync()
            if not buffer:
                return None
            if buffer[0] == 0x24:  # '$' channel length payload
                if len(buffer) < 4:
                    return None
                size = int.from_bytes(buffer[2:4], "big")
                if len(buffer) < 4 + size:
                    return None
                frame = Interleaved(channel=buffer[1], payload=bytes(buffer[4 : 4 + size]))
                del buffer[: 4 + size]
                return frame
            end = buffer.find(b"\r\n\r\n", 0, self._max_header + 4)
            if end < 0:
                if len(buffer) > self._max_header:
                    raise ProtocolError("request header too large")
                return None
            request = parse_request(bytes(buffer[:end]).decode("utf-8", errors="replace"))
            length = 0
            if request is not None and "content-length" in request.headers:
                try:
                    length = int(request.headers["content-length"])
                except ValueError as exc:
                    raise ProtocolError("bad Content-Length") from exc
                if not 0 <= length <= self._max_body:
                    raise ProtocolError("request body too large")
            if len(buffer) < end + 4 + length:
                return None
            body = bytes(buffer[end + 4 : end + 4 + length])
            del buffer[: end + 4 + length]
            if request is None:
                continue
            request.body = body
            return request


def response(status: str, cseq: str, headers: dict[str, str] | None = None, body: str = "") -> bytes:
    fields = {"CSeq": cseq, "Server": "cuckoo"}
    fields.update(headers or {})
    if body:
        fields.setdefault("Content-Type", "application/sdp")
        fields["Content-Length"] = str(len(body.encode()))
    head = f"RTSP/1.0 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items())
    return (head + "\r\n" + body).encode()


def interleaved_frame(channel: int, packet: bytes) -> bytes:
    return b"$" + bytes([channel]) + len(packet).to_bytes(2, "big") + packet


# ---------------------------------------------------------------------- sessions

SESSION_TIMEOUT: Final = 60  # advertised in the Session header, seconds
SEND_TIMEOUT: Final = 10.0  # one blocked write this long and the client is dropped
SR_INTERVAL: Final = 2.0  # seconds between sender reports per track
STALL_TEARDOWN: Final = 10.0  # no upstream frames this long ends playing sessions
LOG_INTERVAL: Final = 60.0  # repeated per-peer INFO notices are folded into DEBUG


class _RateLimitedLog:
    """INFO once per key per interval; the rest at DEBUG, so journald stays quiet."""

    def __init__(self, interval: float = LOG_INTERVAL) -> None:
        self._interval = interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def info(self, key: str, message: str, *args: object) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._last) > 1024:
                self._last.clear()
            loud = now - self._last.get(key, -self._interval) >= self._interval
            if loud:
                self._last[key] = now
        log.log(logging.INFO if loud else logging.DEBUG, message, *args)


notices = _RateLimitedLog()


@dataclass
class TrackState:
    """One SETUP: where the track goes, its packetiser, and what the client says."""

    track: int
    url: str
    transport: Transport
    packetiser: Packetiser
    last_report: float = 0.0
    reception: ReceptionReport | None = None


@dataclass
class ClientSession:
    """One RTSP session: the tracks it set up and the counters behind its stats."""

    identifier: str
    stream: media.Stream
    tracks: dict[int, TrackState] = field(default_factory=dict)
    subscriber: media.Subscriber | None = None
    playing: bool = False
    rtp_socket: socket.socket | None = None
    rtcp_socket: socket.socket | None = None
    bytes_sent: int = 0
    frames_sent: int = 0
    udp_drops: int = 0

    def audio_clock(self) -> int:
        audio = self.stream.audio
        return audio[1] if audio is not None else 16_000

    def new_packetiser(self, track: int) -> Packetiser:
        return Packetiser(
            payload_type=VIDEO_PAYLOAD_TYPE if track == VIDEO_TRACK else AUDIO_PAYLOAD_TYPE,
            clock=VIDEO_CLOCK if track == VIDEO_TRACK else self.audio_clock(),
            ssrc=random.getrandbits(32),
            sequence=random.getrandbits(16),
            timestamp_base=random.getrandbits(32),
        )

    def track_for_rtcp_channel(self, channel: int) -> TrackState | None:
        for state in self.tracks.values():
            if state.transport.interleaved is not None and state.transport.interleaved[1] == channel:
                return state
        return None

    def record(self, summary: RtcpSummary, now: float) -> None:
        for report in summary.reports:
            for state in self.tracks.values():
                if state.packetiser.ssrc == report.ssrc:
                    state.reception = report

    def close_sockets(self) -> None:
        for sock in (self.rtp_socket, self.rtcp_socket):
            if sock is not None:
                sock.close()
        self.rtp_socket = self.rtcp_socket = None


def udp_pair(bind_host: str, attempts: int = 64) -> tuple[socket.socket, socket.socket]:
    """An even RTP port and the RTCP port above it (RFC 3550 §11)."""
    for _ in range(attempts):
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp.bind((bind_host, 0))
        port = rtp.getsockname()[1]
        if port % 2 or port >= 65535:
            rtp.close()
            continue
        rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rtcp.bind((bind_host, port + 1))
        except OSError:
            rtp.close()
            rtcp.close()
            continue
        rtp.setblocking(False)
        rtcp.setblocking(False)
        return rtp, rtcp
    raise OSError("no free RTP/RTCP UDP port pair")


class _Handler(socketserver.BaseRequestHandler):
    """One connected client.

    Requests and the client's interleaved RTCP are read on the handler thread;
    media and sender reports go out on a per-client sender thread. Ingest only
    ever *offers* frames to this client's bounded subscriber, so however slow
    the client is, the camera path and the other clients never wait for it.
    """

    def setup(self) -> None:
        self.session: ClientSession | None = None
        self.write_lock = threading.Lock()
        self.sender: threading.Thread | None = None
        self.stopping = threading.Event()
        self.closed = threading.Event()
        self.reader = ControlReader()
        self.peer = str(self.client_address[0])
        self.connected_at = time.monotonic()
        self.last_seen = self.connected_at
        self.end_reason = "client closed"
        sock = self.request
        assert isinstance(sock, socket.socket)
        # Bounded writes: a peer that stops reading is dropped, not waited on.
        sock.settimeout(SEND_TIMEOUT)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:  # pragma: no cover - platform dependent
            pass
        server = self.server
        assert isinstance(server, RtspServer)
        server.register(self)
        log.debug("rtsp client %s connected", self.peer)

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, RtspServer)
        sock = self.request
        assert isinstance(sock, socket.socket)
        try:
            while not server.closing.is_set() and not self.closed.is_set():
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    if self._idle_expired():
                        self.end_reason = "session timed out"
                        break
                    continue
                if not chunk:
                    break
                self.last_seen = time.monotonic()
                try:
                    items = self.reader.feed(chunk)
                except ProtocolError as exc:
                    self.end_reason = f"protocol error: {exc}"
                    try:
                        self._send(sock, response("400 Bad Request", "0"))
                    except OSError:
                        pass
                    break
                for item in items:
                    if isinstance(item, Interleaved):
                        self._interleaved(item)
                        continue
                    log.debug("<- %s %s", item.method, item.uri)
                    self._respond(server, sock, item)
                    if self.closed.is_set():
                        break
        except OSError as exc:
            if not self.closed.is_set():
                self.end_reason = f"socket error: {exc}"
        finally:
            self._stop_sender()
            self._shutdown(sock)
            session = self.session
            if session is not None:
                if session.subscriber is not None:
                    session.stream.unsubscribe(session.subscriber)
                session.close_sockets()
            server.unregister(self)
            stats = self.stats()
            log.debug(
                "rtsp client %s gone (%s): stream=%s bytes=%s dropped=%s",
                self.peer, self.end_reason, stats.get("stream"),
                stats.get("bytes_sent"), stats.get("frames_dropped"),
            )

    # ------------------------------------------------------------------ helpers

    def _idle_expired(self) -> bool:
        """Only sessions with no other liveness signal time out.

        A TCP-interleaved player is alive as long as it drains its socket (the
        send timeout covers that), so go2rtc need not send keepalives. A UDP
        player, or a connection that never started playing, must keep talking.
        """
        idle = time.monotonic() - self.last_seen
        session = self.session
        if session is None or not session.playing:
            return idle > SESSION_TIMEOUT
        if any(state.transport.is_tcp for state in session.tracks.values()):
            return False
        return idle > SESSION_TIMEOUT * 1.5

    def _shutdown(self, sock: socket.socket) -> None:
        self.closed.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _stop_sender(self) -> None:
        self.stopping.set()
        sender = self.sender
        if sender is not None and sender is not threading.current_thread():
            sender.join(timeout=SEND_TIMEOUT + 1)

    def close(self, reason: str) -> None:
        """End this client from outside (server shutdown): BYE, then hang up."""
        self.end_reason = reason
        self._stop_sender()
        sock = self.request
        assert isinstance(sock, socket.socket)
        try:
            self._send_bye(sock)
        except OSError:
            pass
        self._shutdown(sock)

    def stats(self) -> dict[str, object]:
        session = self.session
        out: dict[str, object] = {
            "peer": self.peer,
            "connected_s": round(time.monotonic() - self.connected_at, 1),
            "stream": session.stream.name if session else None,
            "playing": bool(session and session.playing),
            "transport": None,
            "bytes_sent": session.bytes_sent if session else 0,
            "frames_sent": session.frames_sent if session else 0,
            "frames_dropped": 0,
            "overflows": 0,
            "udp_drops": session.udp_drops if session else 0,
            "tracks": {},
        }
        if session is None:
            return out
        if session.subscriber is not None:
            out["frames_dropped"] = session.subscriber.dropped
            out["overflows"] = session.subscriber.overflows
        if session.tracks:
            out["transport"] = "tcp" if any(
                s.transport.is_tcp for s in session.tracks.values()
            ) else "udp"
        tracks: dict[str, object] = {}
        for track, state in sorted(session.tracks.items()):
            report = state.reception
            tracks["video" if track == VIDEO_TRACK else "audio"] = {
                "packets": state.packetiser.packets,
                "octets": state.packetiser.octets,
                "rr_fraction_lost": report.fraction_lost if report else None,
                "rr_cumulative_lost": report.cumulative_lost if report else None,
                "rr_jitter_ms": (
                    round(report.jitter * 1000 / state.packetiser.clock, 2) if report else None
                ),
            }
        out["tracks"] = tracks
        return out

    # ------------------------------------------------------------------- methods

    def _interleaved(self, item: Interleaved) -> None:
        """Binary data from the client: RTCP reports on a track's odd channel."""
        session = self.session
        if session is None:
            return
        state = session.track_for_rtcp_channel(item.channel)
        if state is None:
            return  # RTP from a client (rare, e.g. NAT keepalives): ignore
        summary = parse_rtcp(item.payload)
        session.record(summary, time.monotonic())
        if summary.bye:
            log.debug("rtsp client %s sent RTCP BYE", self.peer)

    def _session_headers(self, request: Request) -> dict[str, str]:
        if self.session is not None and request.session:
            return {"Session": self.session.identifier}
        return {}

    def _respond(self, server: RtspServer, sock: socket.socket, request: Request) -> None:
        if request.method.startswith("RTSP/"):
            return  # a reply to something we never send; ignore
        if server.auth is not None and not server.auth.valid(
            request.headers.get("authorization", ""), request.method, request.uri,
            str(self.client_address[0]),
        ):
            self._send(sock, response("401 Unauthorized", request.cseq, {
                "WWW-Authenticate": server.auth.challenge(),
            }))
            return
        if request.session and (
            (self.session is not None and request.session != self.session.identifier)
            or (self.session is None and request.method != "SETUP")
        ):
            self._send(sock, response("454 Session Not Found", request.cseq))
            return
        methods = "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"
        if request.method == "OPTIONS":  # also a keepalive
            headers = {"Public": methods} | self._session_headers(request)
            self._send(sock, response("200 OK", request.cseq, headers))
            return
        if request.method == "DESCRIBE":
            self._describe(server, sock, request)
            return
        if request.method == "SETUP":
            self._setup(server, sock, request)
            return
        if request.method == "PLAY":
            self._play(sock, request)
            return
        if request.method == "TEARDOWN":
            self._teardown(sock, request)
            return
        if request.method == "GET_PARAMETER":  # keepalive
            self._send(sock, response("200 OK", request.cseq, self._session_headers(request)))
            return
        self._send(sock, response("501 Not Implemented", request.cseq))

    def _describe(self, server: RtspServer, sock: socket.socket, request: Request) -> None:
        stream = server.hub.get(request.stream_name)
        if stream is None:
            self._send(sock, response("404 Not Found", request.cseq))
            return
        if not stream.live:
            # Honest: no parameter sets yet, or the camera is not pushing this
            # track right now — a session would carry nothing and time out.
            self._send(sock, response("503 Service Unavailable", request.cseq))
            return
        body = sdp(server.advertise_host, request.stream_name, stream)
        self._send(
            sock,
            response("200 OK", request.cseq, {"Content-Base": request.uri.rstrip("/") + "/"}, body),
        )

    def _setup(self, server: RtspServer, sock: socket.socket, request: Request) -> None:
        stream = server.hub.get(request.stream_name)
        track = request.track if request.track is not None else VIDEO_TRACK
        transport = parse_transport(request.headers.get("transport", ""), track)
        if stream is None:
            self._send(sock, response("404 Not Found", request.cseq))
            return
        if transport is None:
            self._send(sock, response("461 Unsupported Transport", request.cseq))
            return
        if not stream.live:
            self._send(sock, response("503 Service Unavailable", request.cseq))
            return
        if track not in (VIDEO_TRACK, AUDIO_TRACK) or (track == AUDIO_TRACK and stream.audio is None):
            self._send(sock, response("404 Not Found", request.cseq))
            return
        session = self.session
        if session is not None and (session.stream is not stream or session.playing):
            status = "455 Method Not Valid in This State" if session.playing else "459 Aggregate Operation Not Allowed"
            self._send(sock, response(status, request.cseq))
            return
        if session is None:
            session = ClientSession(identifier=f"{random.getrandbits(64):016X}", stream=stream)
            self.session = session
        if transport.interleaved is not None and any(
            other.transport.interleaved is not None
            and set(other.transport.interleaved) & set(transport.interleaved)
            for key, other in session.tracks.items() if key != track
        ):
            self._send(sock, response("461 Unsupported Transport", request.cseq))
            return
        server_port = 0
        if not transport.is_tcp:
            transport.udp_host = self.peer
            if session.rtp_socket is None:
                try:
                    session.rtp_socket, session.rtcp_socket = udp_pair(server.bind_host)
                except OSError:
                    self._send(sock, response("453 Not Enough Bandwidth", request.cseq))
                    return
            server_port = session.rtp_socket.getsockname()[1]
        previous = session.tracks.get(track)
        session.tracks[track] = TrackState(
            track=track,
            url=request.uri,
            transport=transport,
            packetiser=previous.packetiser if previous else session.new_packetiser(track),
        )
        self._send(
            sock,
            response(
                "200 OK",
                request.cseq,
                {
                    "Session": f"{session.identifier};timeout={SESSION_TIMEOUT}",
                    "Transport": transport.describe(server_port)
                    + f";ssrc={session.tracks[track].packetiser.ssrc:08X}",
                },
            ),
        )

    def rtp_info(self, session: ClientSession) -> str:
        """Where each track's RTP starts: the next sequence and current RTP time."""
        position = session.stream.position
        return ",".join(
            f"url={state.url};seq={state.packetiser.next_sequence};"
            f"rtptime={state.packetiser.rtp_time(position)}"
            for _, state in sorted(session.tracks.items())
        )

    def _play(self, sock: socket.socket, request: Request) -> None:
        session = self.session
        if session is None or not session.tracks:
            self._send(sock, response("454 Session Not Found", request.cseq))
            return
        headers = {
            "Session": f"{session.identifier};timeout={SESSION_TIMEOUT}",
            "Range": "npt=now-",
        }
        if session.playing:
            self._send(sock, response("200 OK", request.cseq, headers))
            return
        if not session.stream.live:
            self._send(sock, response("503 Service Unavailable", request.cseq))
            return
        # Subscribe first so nothing between this reply and the sender starting
        # is lost; the subscriber holds frames back until the next keyframe.
        session.subscriber = session.stream.subscribe(wait_for_keyframe=True)
        headers["RTP-Info"] = self.rtp_info(session)
        self._send(sock, response("200 OK", request.cseq, headers))
        session.playing = True
        self.sender = threading.Thread(
            target=self._pump, args=(sock, session), daemon=True, name=f"rtsp-{self.peer}"
        )
        self.sender.start()

    def _teardown(self, sock: socket.socket, request: Request) -> None:
        self.end_reason = "teardown"
        self._stop_sender()
        try:
            self._send_bye(sock)
        except OSError:
            pass
        self._send(sock, response("200 OK", request.cseq, self._session_headers(request)))
        self._shutdown(sock)

    # -------------------------------------------------------------------- sending

    def _pump(self, sock: socket.socket, session: ClientSession) -> None:
        subscriber = session.subscriber
        assert subscriber is not None
        ended = False  # True when this thread, not the control thread, ends the session
        try:
            while not self.stopping.is_set():
                frame = subscriber.take(timeout=0.25)
                if self.stopping.is_set():
                    break
                if frame is not None:
                    self._send_frame(sock, session, frame)
                now = time.monotonic()
                self._poll_udp_rtcp(session)
                self._send_reports(sock, session, now)
                age = session.stream.frame_age(now)
                if age is None or age > STALL_TEARDOWN:
                    notices.info(
                        f"stall:{session.stream.name}",
                        "rtsp: %s has had no frames for %.0fs; ending its sessions",
                        session.stream.name, age or 0.0,
                    )
                    self.end_reason = "upstream stalled"
                    ended = True
                    self._send_bye(sock)
                    break
        except TimeoutError:
            ended = True
            self.end_reason = f"send blocked for {SEND_TIMEOUT:.0f}s"
            notices.info(
                f"slow:{self.peer}", "rtsp client %s too slow (send blocked %.0fs); disconnecting",
                self.peer, SEND_TIMEOUT,
            )
        except OSError as exc:
            ended = True
            self.end_reason = f"send failed: {exc}"
        if ended:  # wake the reader so the whole connection ends
            self.stopping.set()
            self._shutdown(sock)

    def _send_frame(self, sock: socket.socket, session: ClientSession, frame: media.Frame) -> None:
        track = VIDEO_TRACK if isinstance(frame, media.VideoFrame) else AUDIO_TRACK
        state = session.tracks.get(track)
        if state is None:
            return
        packetiser = state.packetiser
        if isinstance(frame, media.VideoFrame):
            packets = list(packetiser.video(frame, session.stream.parameters, session.stream.fmt))
        else:
            packets = list(packetiser.audio(frame))
        self._deliver(sock, session, state, packets, rtcp=False)
        session.frames_sent += 1

    def _deliver(
        self, sock: socket.socket, session: ClientSession, state: TrackState,
        packets: list[bytes], rtcp: bool,
    ) -> None:
        transport = state.transport
        if transport.interleaved is not None:
            channel = transport.interleaved[1 if rtcp else 0]
            data = b"".join(interleaved_frame(channel, packet) for packet in packets)
            self._send(sock, data)
            session.bytes_sent += len(data)
            return
        udp = session.rtcp_socket if rtcp else session.rtp_socket
        port = transport.udp_rtcp_port if rtcp else transport.udp_port
        if udp is None or not port:
            return
        for packet in packets:
            try:
                udp.sendto(packet, (transport.udp_host, port))
                session.bytes_sent += len(packet)
            except (BlockingIOError, InterruptedError):
                session.udp_drops += 1

    def _send_reports(self, sock: socket.socket, session: ClientSession, now: float) -> None:
        for state in session.tracks.values():
            if now - state.last_report < SR_INTERVAL:
                continue
            packetiser = state.packetiser
            rtp_time = packetiser.rtp_now(now)
            if rtp_time is None:
                continue  # nothing sent yet: no RTP time to map
            state.last_report = now
            report = sender_report(
                packetiser.ssrc, ntp_timestamp(time.time()), rtp_time,
                packetiser.packets, packetiser.octets,
            )
            self._deliver(sock, session, state, [report], rtcp=True)

    def _send_bye(self, sock: socket.socket) -> None:
        session = self.session
        if session is None or not session.playing:
            return
        now = time.monotonic()
        for state in session.tracks.values():
            packetiser = state.packetiser
            rtp_time = packetiser.rtp_now(now)
            report = sender_report(
                packetiser.ssrc, ntp_timestamp(time.time()), rtp_time or 0,
                packetiser.packets, packetiser.octets,
            ) if rtp_time is not None else b""
            self._deliver(sock, session, state, [report + goodbye(packetiser.ssrc)], rtcp=True)

    def _poll_udp_rtcp(self, session: ClientSession) -> None:
        udp = session.rtcp_socket
        if udp is None:
            return
        for _ in range(16):
            try:
                data, (host, _port) = udp.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if host != self.peer:
                continue
            self.last_seen = time.monotonic()
            session.record(parse_rtcp(data), self.last_seen)

    def _send(self, sock: socket.socket, payload: bytes) -> None:
        with self.write_lock:
            sock.sendall(payload)


class RtspServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, hub: media.Hub, advertise_host: str, port: int = RTSP_PORT,
                 auth: RtspAuth | None = None, bind_host: str = "0.0.0.0") -> None:
        self.hub = hub
        self.advertise_host = advertise_host
        self.auth = auth
        self.bind_host = bind_host
        self.closing = threading.Event()
        self._handlers: set[_Handler] = set()
        self._handlers_lock = threading.Lock()
        super().__init__((bind_host, port), _Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def register(self, handler: _Handler) -> None:
        with self._handlers_lock:
            self._handlers.add(handler)

    def unregister(self, handler: _Handler) -> None:
        with self._handlers_lock:
            self._handlers.discard(handler)

    def client_stats(self) -> dict[str, list[dict[str, object]]]:
        """Per-stream list of connected RTSP clients and their counters."""
        with self._handlers_lock:
            handlers = list(self._handlers)
        out: dict[str, list[dict[str, object]]] = {}
        for handler in handlers:
            stats = handler.stats()
            name = stats.get("stream")
            if isinstance(name, str):
                out.setdefault(name, []).append(stats)
        return out

    def start(self) -> None:
        log.info("rtsp listening on :%d", self.port)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.closing.set()
        self.shutdown()
        with self._handlers_lock:
            handlers = list(self._handlers)
        for handler in handlers:
            handler.close("server stopping")
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def telemetry(hub: media.Hub, server: RtspServer) -> dict[str, dict[str, object]]:
    """Hub stats per track, with that track's RTSP clients folded in."""
    out = hub.stats()
    for name, clients in server.client_stats().items():
        if name in out:
            out[name]["rtsp_clients"] = clients
    return out
