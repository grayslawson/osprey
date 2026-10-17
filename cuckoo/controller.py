"""The camera-facing control channel.

The camera dials us. We accept TLS, complete the WebSocket upgrade, and answer
its hello — that reply is what unblocks adoption. Afterwards we drive the
ack-gated settings suite and keep pinging, because a controller that never pings
is judged dead and the camera resets the channel.

Two sockets arrive on the same path. The management channel negotiates
`secure_transfer`; the PTZ channel negotiates `ptz1` and is only opened after we
ask for it.
"""

from __future__ import annotations

import logging
import queue
import re
import selectors
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final

import adoption
import events
import media
import ptz
import snapshots
from presets import MAX_PRESETS_PER_CAMERA, PresetStore, is_home
from unifiwire import ws
from unifiwire import envelope
from unifiwire.envelope import CAMERA, HELLO, PARAM_AGREEMENT, TIME_SYNC, Envelope, Ids, decode
from model import Camera, Codec, Position, Preset

CONTROL_PORT: Final = 7442
READ_SIZE: Final = 65536
SELECT_TIMEOUT_SEC: Final = 0.5
CONTROL_SUBPROTOCOLS: Final = frozenset({"secure_transfer", ptz.PTZ_SUBPROTOCOL})

# The camera opens its dedicated ptz1 socket only in answer to EnablePtzControl,
# and it does not reliably re-dial after a drop — a stale socket from a previous
# controller leaves it silent and every ONVIF move refused. A missing socket is
# re-armed with a bounded, backing-off Disable/Enable pair.
PTZ_RECOVERY_CALLBACK_TIMEOUT_SEC: Final = 15.0
PTZ_RECOVERY_INITIAL_BACKOFF_SEC: Final = 2.0
PTZ_RECOVERY_MAX_BACKOFF_SEC: Final = 30.0
PTZ_RECOVERY_MAX_ATTEMPTS: Final = 5
# An exhausted recovery is not permanent: a camera that rebooted or recovered
# its network gets another bounded sequence after this long.
PTZ_RECOVERY_REARM_SEC: Final = 300.0
# How long a preset write waits for the camera's acknowledgement. The G5's
# reply shape for `Preset` config is not fully characterised, so a missing ack
# is logged but not treated as a refusal; an explicit error reply is.
PRESET_ACK_TIMEOUT_SEC: Final = 2.0

log = logging.getLogger("cuckoo.control")


@dataclass(frozen=True)
class PtzWatchdog:
    """Liveness bounds for the ptz1 channel.

    The camera's websocket can stay ESTABLISHED, pings and all, while its PTZ
    process has stopped answering. Socket liveness therefore proves nothing:
    only a motor-state push or a GetCurrentPosition reply does. The ptz1 socket
    is recycled, so the bounded Disable/Enable recovery re-establishes it, when
    either `max_unanswered` position requests have gone unanswered, or the
    oldest unanswered request is `reply_timeout_sec` old (silence).
    """

    reply_timeout_sec: float = 30.0
    max_unanswered: int = 3
    # While state is UNKNOWN (motor timeout, fresh channel) or a request is
    # outstanding, re-ask at most this often.
    probe_interval_sec: float = 2.0
    # A healthy idle channel is still probed this often so a silent failure is
    # noticed before the next move needs it. 0 disables idle probing.
    idle_probe_sec: float = 60.0

    def __post_init__(self) -> None:
        if not 0.5 <= self.reply_timeout_sec <= 300:
            raise ValueError("ptz watchdog reply timeout must be 0.5..300 s")
        if not 1 <= self.max_unanswered <= 20:
            raise ValueError("ptz watchdog max_unanswered must be 1..20")
        if not 0.1 <= self.probe_interval_sec <= 60:
            raise ValueError("ptz watchdog probe interval must be 0.1..60 s")
        if not (self.idle_probe_sec == 0 or 5 <= self.idle_probe_sec <= 3600):
            raise ValueError("ptz watchdog idle probe must be 0 or 5..3600 s")


def normalise_mac(value: str) -> str:
    mac = re.sub(r"[:-]", "", value).upper()
    if re.fullmatch(r"[0-9A-F]{12}", mac) is None:
        raise ValueError(f"invalid camera MAC: {value!r}")
    return mac

Handler = Callable[["Session", Envelope], None]


@dataclass
class PtzRecovery:
    """One bounded callback-recovery sequence for a camera."""

    attempts: int = 0
    waiting_for_callback: bool = False
    callback_deadline: float = 0.0
    next_attempt_at: float = 0.0
    exhausted: bool = False
    exhausted_at: float = 0.0


@dataclass
class Session:
    """One camera connection and the state it carries."""

    sock: socket.socket
    upgrade: ws.Upgrade
    camera: Camera
    ids: Ids = field(default_factory=Ids)
    reader: ws.FrameReader = field(default_factory=ws.FrameReader)
    sequence: adoption.Sequence | None = None
    last_ping: float = 0.0
    ack_deadline: float = 0.0
    ptz_ready: bool = False
    # Set when the peer sent a CLOSE frame or a write failed mid-frame; the read
    # loop then drops the session through the normal _drop() path.
    closing: bool = False
    broken: bool = False
    # PTZ liveness: monotonic time of the oldest unanswered position request,
    # how many are outstanding, and when we last asked.
    position_requested_at: float = 0.0
    unanswered_polls: int = 0
    last_probe_at: float = 0.0
    # Set by the controller: every message in and out, for when the question is
    # "what did we actually send it?".
    traffic: Callable[[str, Envelope], None] | None = None
    move_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def is_ptz_channel(self) -> bool:
        return self.upgrade.subprotocol == ptz.PTZ_SUBPROTOCOL

    def send(self, message: Envelope) -> None:
        with self._write_lock:
            self._send_unlocked(message)

    def _send_unlocked(self, message: Envelope) -> None:
        log.debug("-> %s id=%s", message.function_name, message.message_id)
        if self.traffic is not None:
            self.traffic("out", message)
        self._write_frame(message.to_json(), ws.Opcode.BINARY)

    def _write_frame(self, payload: bytes, opcode: ws.Opcode) -> None:
        """Write one whole frame; any failure poisons the stream.

        A timeout or error part-way through `sendall` leaves a partial frame
        on the wire, after which nothing we send can be parsed. Mark the session
        broken and shut the socket down so the read loop drops it and recovery
        starts, instead of writing more bytes into a desynchronised stream.
        """
        if self.broken:
            raise OSError("session stream is broken")
        try:
            ws.send(self.sock, payload, opcode)
        except OSError:
            self.broken = True
            shutdown = getattr(self.sock, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            raise

    def ping(self) -> None:
        with self._write_lock:
            self._write_frame(b"", ws.Opcode.PING)

    def pong(self, payload: bytes) -> None:
        with self._write_lock:
            self._write_frame(payload, ws.Opcode.PONG)

    def request_position(
        self, now: float | None = None, coalesce_sec: float = 0.0
    ) -> Envelope | None:
        """Ask for the motor pose and count it as outstanding until answered.

        With `coalesce_sec`, a request made while another is still outstanding
        and younger than that is folded into it (returns None). On-demand
        GetStatus refreshes use this: Frigate polls several times a second while
        tracking, and counting each poll would let a merely slow camera trip the
        watchdog's unanswered-request bound.
        """
        at = time.monotonic() if now is None else now
        with self._write_lock:
            if (
                coalesce_sec > 0
                and self.unanswered_polls > 0
                and at - self.last_probe_at < coalesce_sec
            ):
                return None
            message = envelope.request(ptz.GET_POSITION, ptz.get_position(), self.ids)
            self._send_unlocked(message)
            if self.unanswered_polls == 0:
                self.position_requested_at = at
            self.unanswered_polls += 1
            self.last_probe_at = at
            return message

    def note_motor_reply(self) -> None:
        with self._write_lock:
            self.unanswered_polls = 0
            self.position_requested_at = 0.0

    def reply(self, source: Envelope, payload: dict[str, Any]) -> None:
        with self._write_lock:
            self._send_unlocked(adoption.build_reply(source, payload, self.ids))

    def request(self, name: str, payload: dict[str, Any]) -> Envelope:
        with self._write_lock:
            message = envelope.request(name, payload, self.ids)
            self._send_unlocked(message)
            return message

    def request_many(self, name: str, payloads: list[dict[str, Any]]) -> list[Envelope]:
        """Write one logical command's frames without another writer interleaving."""
        messages: list[Envelope] = []
        with self._write_lock:
            for payload in payloads:
                message = envelope.request(name, payload, self.ids)
                self._send_unlocked(message)
                messages.append(message)
        return messages


class Controller:
    """Accepts camera connections and runs the protocol over them."""

    def __init__(
        self,
        cert: Path,
        ingest_host: str,
        control_port: int = CONTROL_PORT,
        ingest_port: int = 7550,
        tracks: list[str] | None = None,
        track_codecs: dict[str, Codec] | None = None,
        name: str = "cuckoo",
        hub: media.Hub | None = None,
        images: snapshots.Store | None = None,
        expected_camera_ip: str | None = None,
        expected_camera_mac: str | None = None,
        configured_cameras: dict[str, dict[str, object]] | None = None,
        bind_host: str = "0.0.0.0",
        preset_store: PresetStore | None = None,
        auto_home: bool = True,
        watchdog: PtzWatchdog | None = None,
        preset_ack_timeout: float = PRESET_ACK_TIMEOUT_SEC,
    ) -> None:
        self.cert = cert
        self.ingest_host = ingest_host
        self.control_port = control_port
        self.ingest_port = ingest_port
        self.tracks = tracks or ["video1"]
        self.track_codecs = track_codecs or {}
        self.name = name
        self.hub = hub
        self.images = images
        self.expected_camera_ip = expected_camera_ip
        self.expected_camera_mac = (
            normalise_mac(expected_camera_mac) if expected_camera_mac else None
        )
        self.bind_host = bind_host
        self.configured_cameras = configured_cameras or {}
        self.cameras: dict[str, Camera] = {}
        self._sessions: dict[socket.socket, Session] = {}
        self._sessions_lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._ready: queue.Queue[Session] = queue.Queue()
        # A recovery is owned per camera, never by each timer tick or socket.
        self._ptz_recovery: dict[str, PtzRecovery] = {}
        self.preset_store = preset_store
        self.auto_home = auto_home
        self.watchdog = watchdog or PtzWatchdog()
        self.preset_ack_timeout = preset_ack_timeout
        self._presets_lock = threading.Lock()
        self._auto_home_checked: set[str] = set()
        self._presets_synced: set[str] = set()
        # Consecutive watchdog recycles without a motor reply, per camera. They
        # count towards the recovery attempt budget so a camera that re-dials
        # but never answers is backed off rather than recycled every 5 s.
        self._watchdog_strikes: dict[str, int] = {}
        self._watchdog_recycles: dict[str, int] = {}
        self._recover_requests: queue.Queue[str] = queue.Queue()
        self._acks: dict[int, tuple[threading.Event, list[Envelope]]] = {}
        self._acks_lock = threading.Lock()
        self._selector: selectors.BaseSelector | None = None
        self.on_adopted: Callable[[Camera], None] | None = None
        self.on_motion: Callable[[Camera], None] | None = None
        self.on_detection: Callable[[Camera, events.Detection], None] | None = None
        self.on_traffic: Callable[[str, Envelope], None] | None = None

    # ------------------------------------------------------------------ lifecycle

    def stop(self) -> None:
        self._stop.set()

    def serve_forever(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert))
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.bind_host, self.control_port))
        listener.listen(8)
        self._listener = listener
        log.info("control channel listening on :%d", self.control_port)

        sel = selectors.DefaultSelector()
        sel.register(listener, selectors.EVENT_READ)
        self._selector = sel
        try:
            while not self._stop.is_set():
                for key, _ in sel.select(timeout=SELECT_TIMEOUT_SEC):
                    if key.fileobj is listener:
                        self._spawn_accept(listener, context)
                    else:
                        assert isinstance(key.fileobj, socket.socket)
                        self._readable(key.fileobj, sel)
                self._drain_ready(sel)
                self._tick(sel)
        finally:
            sel.close()
            listener.close()

    def _spawn_accept(self, listener: socket.socket, context: ssl.SSLContext) -> None:
        try:
            raw, peer = listener.accept()
        except OSError:  # pragma: no cover - listener closing
            return
        # Each connection is upgraded on its own thread so a burst cannot starve
        # the one carrying the hello. The thread hands the ready session back to
        # the single read loop, which keeps dispatch single-threaded as the
        # Session state machine assumes.
        threading.Thread(
            target=self._handshake, args=(raw, peer, context), daemon=True
        ).start()

    def _handshake(
        self, raw: socket.socket, peer: tuple[str, int], context: ssl.SSLContext
    ) -> None:
        try:
            sock = context.wrap_socket(raw, server_side=True)
            sock.settimeout(10.0)
            request = sock.recv(READ_SIZE)
            upgrade = ws.parse_upgrade(request)
            if upgrade.path != ws.CONTROL_PATH:
                raise ws.ProtocolError("unexpected websocket path")
            if upgrade.subprotocol not in CONTROL_SUBPROTOCOLS:
                raise ws.ProtocolError("unsupported camera websocket subprotocol")
            try:
                camera_mac = normalise_mac(upgrade.camera_mac)
            except ValueError as exc:
                raise ws.ProtocolError("camera-mac header is required and must be a MAC") from exc
            if (
                self.expected_camera_ip is not None
                and peer[0] != self.expected_camera_ip
            ):
                raise ws.ProtocolError(f"unexpected camera source IP {peer[0]}")
            if self.expected_camera_mac is not None:
                try:
                    camera_mac = normalise_mac(upgrade.camera_mac)
                except ValueError as exc:
                    raise ws.ProtocolError(str(exc)) from exc
                if camera_mac != self.expected_camera_mac:
                    raise ws.ProtocolError(f"unexpected camera MAC {camera_mac}")
            sock.sendall(ws.handshake_response(upgrade))
            # Selector-driven reads must never block indefinitely on TLS.
            sock.settimeout(0.5)
        except (ssl.SSLError, OSError, ws.ProtocolError) as exc:
            preview = locals().get("request", b"")
            if isinstance(preview, bytes) and preview:
                log.warning(
                    "rejected connection from %s: %s — first bytes: %s | %s",
                    peer[0], exc, preview[:48].hex(" "),
                    "".join(chr(b) if 32 <= b < 127 else "." for b in preview[:48]),
                )
            else:
                log.warning("rejected connection from %s: %s", peer[0], exc)
            raw.close()
            return

        mac = camera_mac
        if self.configured_cameras and mac not in self.configured_cameras:
            log.warning("rejected unregistered camera: mac=%s model=%s", mac, upgrade.camera_model)
            sock.close()
            return
        camera = self._camera_for(mac)
        registration = self.configured_cameras.get(mac)
        if registration is not None:
            camera.name = str(registration.get("name", camera.name or mac))
        camera.model = upgrade.camera_model or camera.model
        camera.firmware = upgrade.camera_firmware or camera.firmware
        if not camera.tracks:
            camera.tracks = adoption.track_defaults()
        adoption.set_track_codecs(camera, self.track_codecs)

        session = Session(sock=sock, upgrade=upgrade, camera=camera, traffic=self.on_traffic)
        log.info(
            "%s channel up: mac=%s model=%s adopted=%s",
            upgrade.subprotocol or "?", mac, camera.model, upgrade.already_adopted,
        )
        if session.is_ptz_channel:
            session.ptz_ready = True
            session.camera.motion.open_channel()
            # A replacement PTZ socket invalidates any pending move from the
            # old one, and a first socket has not told us where the head is.
            # Query immediately: ONVIF leaves UNKNOWN only after this channel
            # has supplied truthful state, and the watchdog counts the request.
            try:
                session.request_position()
                self._sync_presets(session)
            except (OSError, ssl.SSLError) as exc:
                log.warning("PTZ channel write failed on open: %s", exc)
        self._ready.put(session)

    def _camera_for(self, mac: str) -> Camera:
        """The model for a connecting camera, restoring persisted presets once."""
        camera = self.cameras.get(mac)
        if camera is not None:
            return camera
        camera = Camera(mac=mac)
        if self.preset_store is not None:
            # Restore the slot table this controller wrote before it
            # restarted; the camera keeps the slots but cannot list them.
            camera.presets = self.preset_store.load(mac)
            if camera.presets:
                log.info("restored %d preset(s) for %s", len(camera.presets), mac)
        return self.cameras.setdefault(mac, camera)

    def _drain_ready(self, sel: selectors.BaseSelector) -> None:
        """Register sessions the accept threads finished upgrading."""
        while True:
            try:
                session = self._ready.get_nowait()
            except queue.Empty:
                return
            with self._sessions_lock:
                self._sessions[session.sock] = session
            sel.register(session.sock, selectors.EVENT_READ)

    def _drop(
        self,
        sock: socket.socket,
        sel: selectors.BaseSelector | None = None,
        reason: str = "PTZ channel unavailable",
    ) -> None:
        sel = sel if sel is not None else self._selector
        recovery_control: Session | None = None
        with self._sessions_lock:
            session = self._sessions.pop(sock, None)
            if session is not None and session.is_ptz_channel:
                has_replacement = any(
                    candidate.camera.mac == session.camera.mac and candidate.is_ptz_channel
                    for candidate in self._sessions.values()
                )
                if not has_replacement:
                    session.camera.motion.disconnect(reason)
                    recovery_control = next(
                        (
                            candidate
                            for candidate in self._sessions.values()
                            if candidate.camera.mac == session.camera.mac
                            and not candidate.is_ptz_channel
                        ),
                        None,
                    )
        if session is not None:
            log.info("%s channel closed", session.upgrade.subprotocol or "?")
        if recovery_control is not None:
            self._request_ptz_recovery(recovery_control, time.monotonic())
        if sel is not None:
            try:
                sel.unregister(sock)
            except (KeyError, ValueError):
                pass
        try:
            sock.close()
        except OSError:  # pragma: no cover - already closed
            pass

    # -------------------------------------------------------------------- reading

    def _readable(self, sock: socket.socket, sel: selectors.BaseSelector) -> None:
        session = self._sessions.get(sock)
        if session is None:
            self._drop(sock, sel)
            return
        try:
            chunk = sock.recv(READ_SIZE)
        except (TimeoutError,):
            return
        except (OSError, ssl.SSLError):
            chunk = b""
        if not chunk:
            self._drop(sock, sel)
            return
        for frame in session.reader.feed(chunk):
            if session.closing:
                break
            self._on_frame(session, frame)
        if session.closing or session.broken:
            self._drop(sock, sel)

    def _on_frame(self, session: Session, frame: ws.Frame) -> None:
        if frame.opcode is ws.Opcode.CLOSE:
            # Never close the socket here: a closed-but-registered fd lets the
            # kernel hand the same number to the next accepted socket, and
            # sel.register() then raises and kills the control loop. The read
            # loop drops it through _drop(), which also starts PTZ recovery.
            session.closing = True
            return
        if frame.opcode is ws.Opcode.PING:
            try:
                session.pong(frame.payload)
            except (OSError, ssl.SSLError):
                pass  # broken is set; the read loop drops the session
            return
        if frame.opcode is ws.Opcode.PONG:
            return
        try:
            message = decode(frame.payload)
        except Exception as exc:  # a malformed frame must not kill the session
            log.debug("undecodable frame: %s", exc)
            return
        if message.sender != CAMERA:
            return
        log.debug("<- %s id=%s reply_to=%s",
                  message.function_name, message.message_id, message.in_response_to)
        if self.on_traffic is not None:
            self.on_traffic("in", message)
        self._dispatch(session, message)

    def _dispatch(self, session: Session, message: Envelope) -> None:
        name = message.function_name
        if message.is_reply:
            self._resolve_ack(message)

        if name == TIME_SYNC:
            # Answer EVERY timeSync, reply or not. The camera and controller
            # ping-pong timeSync — each message carries inResponseTo referencing
            # the other's last — until the camera's clock converges, and only then
            # does the camera send its hello. Answering just the first (treating
            # the rest as "replies" to ignore) stalls the exchange, and the camera
            # never introduces itself. The real controller never sends a hello of
            # its own; it waits for the camera's.
            session.reply(message, adoption.time_sync_reply(int(time.time() * 1000)))
            return

        if name == HELLO:
            # Either the camera opened with a hello, or it is answering the one we
            # sent to take it over. Both carry the features we need.
            self._on_hello(session, message)
            return

        if name in (ptz.EVENT_MOTOR_STATE, ptz.GET_POSITION):
            parsed = ptz.parse_motor_state(message.payload)
            if parsed is not None:
                position, activity = parsed
                motion = session.camera.motion
                was_unknown = motion.status == "UNKNOWN"
                motion.update(
                    position,
                    activity,
                    confirmed=name == ptz.GET_POSITION,
                    ignore_activity=bool(message.payload.get("ignoreActivity")),
                )
                session.note_motor_reply()
                if self._watchdog_strikes.pop(session.camera.mac, 0) or was_unknown:
                    log.warning("PTZ motor state fresh again: mac=%s", session.camera.mac)
                if name == ptz.GET_POSITION and session.is_ptz_channel:
                    self._maybe_auto_home(session)
                if self.on_motion is not None:
                    self.on_motion(session.camera)
            if name == ptz.EVENT_MOTOR_STATE:
                return

        if name in events.DETECTION_VERBS:
            for detection in events.parse(name, message.payload):
                log.debug("detection %s active=%s", detection.kind, detection.active)
                if self.on_detection is not None:
                    self.on_detection(session.camera, detection)
            return

        if name == PARAM_AGREEMENT and message.is_reply and message.payload.get("features"):
            # Its answer carries the same `features` block the hello would have,
            # which is where the motor bounds come from. Only when it actually
            # carries one: a bare acknowledgement must not erase what we know.
            adoption.apply_hello(session.camera, message.payload)
            log.info(
                "features from paramAgreement: ptz=%s pan=%s..%s",
                session.camera.is_ptz,
                session.camera.pan_range.minimum,
                session.camera.pan_range.maximum,
            )

        if message.is_reply and session.sequence is not None:
            if session.sequence.on_reply(message):
                self._advance(session)
            return

    def _on_hello(self, session: Session, message: Envelope) -> None:
        """Reply first — that is the gate — then agree parameters and settle in."""
        if session.sequence is not None:
            return  # already under way; a second hello is not a second adoption
        adoption.apply_hello(session.camera, message.payload)
        if not message.is_reply:
            # Only answer a hello the camera started. Replying to its answer to
            # ours would be an endless exchange of pleasantries.
            session.reply(message, adoption.hello_reply(self.name))
        session.request(PARAM_AGREEMENT, adoption.param_agreement())
        session.sequence = adoption.Sequence(
            steps=adoption.suite(
                session.camera, self.ingest_host, self.ingest_port, self.tracks
            ),
            ids=session.ids,
        )
        self._advance(session)

    def _advance(self, session: Session) -> None:
        sequence = session.sequence
        if sequence is None:
            return
        message = sequence.next_message()
        if message is not None:
            session.send(message)
            session.ack_deadline = time.time() + adoption.ACK_TIMEOUT_SEC
            return
        if sequence.done:
            # A camera process loses its dedicated ptz1 socket when it restarts.
            # Re-issue the callback URL after every completed management-channel
            # setup, not only on first adoption, so it can recreate that socket.
            if session.camera.is_ptz:
                self._enable_ptz(session)
                self._wait_for_ptz_callback(session.camera.mac, time.monotonic())
            if not session.camera.adopted:
                session.camera.adopted = True
                log.info("adopted %s", session.camera.mac)
                if self.on_adopted is not None:
                    self.on_adopted(session.camera)

    def _ptz_url(self) -> str:
        """Our management URL, which the camera dials back on for its PTZ socket.

        The port has to be in the URL whenever we are not on the default one, or
        the camera dials 7442 — which on a host already running a controller is
        somebody else's socket.
        """
        authority = self.ingest_host
        if self.control_port != CONTROL_PORT:
            authority = f"{self.ingest_host}:{self.control_port}"
        return f"wss://{authority}{ws.CONTROL_PATH}"

    def _enable_ptz(self, session: Session) -> None:
        """Hand the camera our own URL; it dials back on the ptz1 subprotocol."""
        session.request(ptz.ENABLE_PTZ, ptz.enable(self._ptz_url()))

    def _wait_for_ptz_callback(self, mac: str, now: float) -> None:
        """Record the bounded wait after an EnablePtzControl command."""
        recovery = self._ptz_recovery.get(mac)
        if recovery is None:
            recovery = PtzRecovery()
            self._ptz_recovery[mac] = recovery
        if recovery.waiting_for_callback or recovery.exhausted:
            return
        recovery.waiting_for_callback = True
        recovery.callback_deadline = now + PTZ_RECOVERY_CALLBACK_TIMEOUT_SEC
        log.info("PTZ recovery waiting for callback: mac=%s", mac)

    def _request_ptz_recovery(self, session: Session, now: float) -> None:
        """Start the one recovery owner after a live ptz1 socket disappears."""
        mac = session.camera.mac
        recovery = self._ptz_recovery.get(mac)
        if recovery is not None and (recovery.waiting_for_callback or recovery.exhausted):
            return
        self._ptz_recovery[mac] = PtzRecovery(next_attempt_at=now)
        log.warning("PTZ channel unavailable: mac=%s; recovery requested", mac)

    def _recover_ptz(self, now: float) -> None:
        """Drive bounded Disable/Enable recovery through management sockets only."""
        with self._sessions_lock:
            sessions = list(self._sessions.values())
        controls = {
            session.camera.mac: session
            for session in sessions
            if not session.is_ptz_channel
        }
        restored = {session.camera.mac for session in sessions if session.is_ptz_channel}

        for mac, recovery in list(self._ptz_recovery.items()):
            if mac in restored:
                log.warning("PTZ channel restored: mac=%s", mac)
                del self._ptz_recovery[mac]
                continue
            session = controls.get(mac)
            if session is None:
                # PTZ recovery is meaningful only while its management channel is
                # live; a new management setup will enable it again.
                del self._ptz_recovery[mac]
                continue
            if recovery.exhausted:
                if now - recovery.exhausted_at < PTZ_RECOVERY_REARM_SEC:
                    continue
                log.warning(
                    "PTZ recovery re-armed after %.0fs: mac=%s", PTZ_RECOVERY_REARM_SEC, mac
                )
                self._watchdog_strikes.pop(mac, None)
                recovery.exhausted = False
                recovery.attempts = 0
                recovery.next_attempt_at = now
            if recovery.waiting_for_callback:
                if now < recovery.callback_deadline:
                    continue
                recovery.waiting_for_callback = False
                backoff = min(
                    PTZ_RECOVERY_INITIAL_BACKOFF_SEC * (2 ** recovery.attempts),
                    PTZ_RECOVERY_MAX_BACKOFF_SEC,
                )
                recovery.next_attempt_at = now + backoff
                log.warning(
                    "PTZ recovery callback timed out: mac=%s; retry in %.1fs",
                    mac,
                    backoff,
                )
                continue
            if now < recovery.next_attempt_at:
                continue
            if recovery.attempts >= PTZ_RECOVERY_MAX_ATTEMPTS:
                recovery.exhausted = True
                recovery.exhausted_at = now
                log.error(
                    "PTZ recovery timed out: mac=%s after %d attempts; "
                    "retrying in %.0fs (or use Recover PTZ)",
                    mac,
                    recovery.attempts,
                    PTZ_RECOVERY_REARM_SEC,
                )
                continue
            recovery.attempts += 1
            log.warning(
                "PTZ recovery requested: mac=%s attempt=%d/%d",
                mac,
                recovery.attempts,
                PTZ_RECOVERY_MAX_ATTEMPTS,
            )
            try:
                session.request(ptz.DISABLE_PTZ, ptz.disable())
                session.request(ptz.ENABLE_PTZ, ptz.enable(self._ptz_url()))
            except (OSError, ssl.SSLError):
                # The control socket will be dropped by the read loop; do not
                # spin a second recovery owner while that happens.
                recovery.next_attempt_at = now + PTZ_RECOVERY_INITIAL_BACKOFF_SEC
                continue
            self._wait_for_ptz_callback(mac, now)

    # --------------------------------------------------------------------- timers

    def _tick(self, sel: selectors.BaseSelector | None = None) -> None:
        now = time.time()
        with self._sessions_lock:
            current = list(self._sessions.values())
        for session in current:
            if session.broken or session.closing:
                self._drop(session.sock, sel)
                continue
            if now - session.last_ping >= adoption.PING_INTERVAL_SEC:
                try:
                    session.ping()
                except (OSError, ssl.SSLError):
                    continue
                session.last_ping = now
            sequence = session.sequence
            if (
                sequence is not None
                and sequence.waiting_for is not None
                and session.ack_deadline
                and now > session.ack_deadline
            ):
                log.debug("no ack for id=%s, stepping past", sequence.waiting_for)
                sequence.on_timeout()
                self._advance(session)
        monotonic = time.monotonic()
        self._drain_recover_requests(monotonic, sel)
        self._watch_ptz(monotonic, sel)
        # Re-read the clock: a drop above schedules recovery at "now".
        self._recover_ptz(time.monotonic())

    # ------------------------------------------------------------------- watchdog

    def _watch_ptz(self, now: float, sel: selectors.BaseSelector | None = None) -> None:
        """Probe and, when it has stopped answering, recycle each ptz1 socket."""
        config = self.watchdog
        with self._sessions_lock:
            ptz_sessions = [s for s in self._sessions.values() if s.is_ptz_channel]
        for session in ptz_sessions:
            if session.broken or session.closing:
                continue
            motion = session.camera.motion
            outstanding = session.unanswered_polls
            silent_for = now - session.position_requested_at if outstanding else 0.0
            if outstanding >= config.max_unanswered or (
                outstanding > 0 and silent_for >= config.reply_timeout_sec
            ):
                self._recycle_ptz(
                    session,
                    now,
                    sel,
                    f"no motor reply for {silent_for:.1f}s "
                    f"after {outstanding} position request(s)",
                )
                continue
            since_probe = now - session.last_probe_at
            age = motion.last_reply_age(now)
            due = (
                (outstanding > 0 or motion.needs_poll)
                and since_probe >= config.probe_interval_sec
            ) or (
                config.idle_probe_sec > 0
                and since_probe >= config.idle_probe_sec
                and (age is None or age >= config.idle_probe_sec)
            )
            if not due:
                continue
            try:
                session.request_position(now)
            except (OSError, ssl.SSLError) as exc:
                log.warning("PTZ position probe failed: mac=%s: %s", session.camera.mac, exc)

    def _recycle_ptz(
        self,
        session: Session,
        now: float,
        sel: selectors.BaseSelector | None,
        why: str,
    ) -> None:
        """Close a ptz1 socket that has stopped answering and back off its recovery."""
        mac = session.camera.mac
        strikes = self._watchdog_strikes.get(mac, 0) + 1
        self._watchdog_strikes[mac] = strikes
        self._watchdog_recycles[mac] = self._watchdog_recycles.get(mac, 0) + 1
        log.error(
            "PTZ watchdog: mac=%s %s; recycling ptz1 channel (strike %d/%d)",
            mac, why, strikes, PTZ_RECOVERY_MAX_ATTEMPTS,
        )
        self._drop(session.sock, sel, reason="PTZ not responding; recovering channel")
        recovery = self._ptz_recovery.get(mac)
        if recovery is not None and strikes > 1 and not recovery.waiting_for_callback:
            # The camera re-dialled last time but still never answered. Spend
            # the shared attempt budget and wait, rather than a tight loop.
            recovery.attempts = max(recovery.attempts, strikes - 1)
            recovery.next_attempt_at = now + min(
                PTZ_RECOVERY_INITIAL_BACKOFF_SEC * (2 ** (strikes - 1)),
                PTZ_RECOVERY_MAX_BACKOFF_SEC,
            )

    def recover_ptz(self, mac: str) -> bool:
        """Operator request: recycle the PTZ channel now (thread-safe).

        Returns False when there is no live management channel to recover
        through. The work happens on the control loop's next tick.
        """
        if self._control_session(mac) is None:
            return False
        log.warning("PTZ recovery requested by operator: mac=%s", mac)
        self._recover_requests.put(mac)
        return True

    def _drain_recover_requests(
        self, now: float, sel: selectors.BaseSelector | None = None
    ) -> None:
        while True:
            try:
                mac = self._recover_requests.get_nowait()
            except queue.Empty:
                return
            # A manual request starts a fresh budget, even after give-up.
            self._watchdog_strikes.pop(mac, None)
            self._ptz_recovery.pop(mac, None)
            ptz_session = self._ptz_session(mac)
            if ptz_session is not None:
                self._watchdog_recycles[mac] = self._watchdog_recycles.get(mac, 0) + 1
                self._drop(ptz_session.sock, sel, reason="operator requested PTZ recovery")
                if mac in self._ptz_recovery:
                    continue
            control = self._control_session(mac)
            if control is not None:
                self._request_ptz_recovery(control, now)

    def ptz_health(self, mac: str, now: float | None = None) -> dict[str, object]:
        """Machine-readable PTZ liveness for /healthz and the operator console."""
        at = time.monotonic() if now is None else now
        camera = self.cameras.get(mac)
        if camera is None:
            return {"ptz": False, "healthy": False, "state": "UNKNOWN", "error": "no camera"}
        session = self._ptz_session(mac)
        motion = camera.motion
        state = motion.status
        recovery = self._ptz_recovery.get(mac)
        age = motion.last_reply_age(at)
        return {
            "ptz": camera.is_ptz,
            "channel": "up" if session is not None else "down",
            "state": state,
            "error": motion.error,
            "healthy": session is not None and state != "UNKNOWN",
            "last_motor_reply_age_sec": round(age, 1) if age is not None else None,
            "unanswered_position_requests": session.unanswered_polls if session else 0,
            "motion_timeouts": motion.timeouts,
            "watchdog_recycles": self._watchdog_recycles.get(mac, 0),
            "recovery": {
                "active": recovery is not None,
                "attempts": recovery.attempts if recovery else 0,
                "max_attempts": PTZ_RECOVERY_MAX_ATTEMPTS,
                "waiting_for_callback": recovery.waiting_for_callback if recovery else False,
                "exhausted": recovery.exhausted if recovery else False,
            },
            "presets": len(camera.presets),
            "home_preset": any(is_home(p.name) for p in camera.presets.values()),
        }


    # ----------------------------------------------------------------------- moves

    def move(self, mac: str, position: Any, speed: int = ptz.DEFAULT_SPEED) -> bool:
        """Point the head somewhere. Returns False if no PTZ channel is up."""
        session = self._ptz_session(mac)
        if session is None:
            return False
        with session.move_lock:
            if not session.camera.motion.begin(position):
                return False
            try:
                session.request_many(ptz.PRESET, ptz.move_to(position, speed))
            except (OSError, ssl.SSLError):
                session.camera.motion.cancel()
                return False
            return True

    def stop_motion(self, mac: str, pan_tilt: bool = True, zoom: bool = True) -> bool:
        """Halt an in-flight move by re-targeting the head at its reported pose.

        The PTZ channel has no stop verb; writing the scratch preset at the
        current position and going there is the closest real equivalent.
        Stopping an idle head is a successful no-op. Returns False only when no
        PTZ channel is available or the command could not be written.
        """
        session = self._ptz_session(mac)
        if session is None:
            return False
        with session.move_lock:
            motion = session.camera.motion
            was_moving = motion.activity != 0
            hold = motion.halt(pan_tilt, zoom)
            if hold is None:
                return False
            if not was_moving:
                return True
            try:
                session.request_many(ptz.PRESET, ptz.move_to(hold))
                session.request(ptz.GET_POSITION, ptz.get_position())
            except (OSError, ssl.SSLError):
                return False
            return True

    def goto_preset(self, mac: str, index: int, speed: int = ptz.DEFAULT_SPEED) -> bool:
        session = self._ptz_session(mac)
        if session is None:
            return False
        preset = session.camera.presets.get(index)
        target = preset.position if preset is not None else None
        with session.move_lock:
            if not session.camera.motion.begin(target):
                return False
            try:
                session.request(ptz.PRESET, ptz.go(index, speed))
            except (OSError, ssl.SSLError):
                session.camera.motion.cancel()
                return False
            return True

    @staticmethod
    def _free_slot(camera: Camera) -> int | None:
        """The lowest slot that is neither in use nor the scratch slot."""
        for slot in range(0, 256):
            if slot != ptz.SCRATCH_INDEX and slot not in camera.presets:
                return slot
        return None

    def set_preset(self, mac: str, name: str, index: int | None = None) -> int | None:
        """Store the current position in a preset slot the camera keeps.

        Returns the slot only once the write has gone out and has not been
        refused by the camera; the table is then persisted.
        """
        session = self._ptz_session(mac)
        camera = self.cameras.get(mac)
        if session is None or camera is None:
            return None
        with self._presets_lock:
            if index is not None and index != ptz.SCRATCH_INDEX and 0 <= index <= 255:
                slot: int | None = index
            else:
                # The scratch slot is overwritten by every arbitrary move, and
                # `slot + 1` could silently replace another named position.
                slot = self._free_slot(camera)
            if slot is None or (
                slot not in camera.presets and len(camera.presets) >= MAX_PRESETS_PER_CAMERA
            ):
                log.warning("preset table full: mac=%s", mac)
                return None
            preset = Preset(index=slot, name=name, position=camera.motion.position)
        acked = self._request_acked(session, ptz.PRESET, ptz.configure(preset))
        if acked is False:
            log.warning("camera refused preset %r in slot %d: mac=%s", name, slot, mac)
            return None
        if acked is None:
            log.warning(
                "preset %r slot %d not acknowledged within %.1fs; keeping it: mac=%s",
                name, slot, self.preset_ack_timeout, mac,
            )
        with self._presets_lock:
            camera.presets[slot] = preset
            self._persist_presets(camera)
        return slot

    def remove_preset(self, mac: str, index: int) -> bool:
        """Forget a preset.

        There is no measured delete action on this verb, so the slot is dropped
        from our own table and left to be overwritten rather than pretending to
        erase it on the camera.
        """
        camera = self.cameras.get(mac)
        if camera is None:
            return False
        with self._presets_lock:
            if index not in camera.presets:
                return False
            del camera.presets[index]
            self._persist_presets(camera)
        return True

    def _persist_presets(self, camera: Camera) -> None:
        if self.preset_store is not None:
            self.preset_store.save(camera.mac, dict(camera.presets))

    def _request_acked(
        self, session: Session, name: str, payload: dict[str, Any]
    ) -> bool | None:
        """Send one request and wait briefly for its reply.

        True: acknowledged. False: refused, or the write failed. None: no reply
        within `preset_ack_timeout`. Never call this from the control loop
        thread, which is the one that would deliver the reply.
        """
        event = threading.Event()
        replies: list[Envelope] = []
        try:
            # Hold the ack table while sending so a reply that races the
            # registration still finds its waiter.
            with self._acks_lock:
                message = session.request(name, payload)
                self._acks[message.message_id] = (event, replies)
        except (OSError, ssl.SSLError):
            return False
        try:
            if not event.wait(self.preset_ack_timeout):
                return None
        finally:
            with self._acks_lock:
                self._acks.pop(message.message_id, None)
        reply = replies[0].payload if replies else {}
        status = reply.get("status")
        refused = (
            bool(reply.get("error"))
            or reply.get("success") is False
            or (isinstance(status, str) and status.lower() in ("error", "fail", "failed"))
        )
        return not refused

    def _resolve_ack(self, message: Envelope) -> None:
        with self._acks_lock:
            waiter = self._acks.get(message.in_response_to)
        if waiter is not None:
            waiter[1].append(message)
            waiter[0].set()

    def _sync_presets(self, session: Session) -> None:
        """Re-assert restored presets on the camera once per process.

        Writing a slot does not move the head, and makes the advertised table
        true even if the camera lost its own copy (factory reset, replacement).
        """
        camera = session.camera
        if camera.mac in self._presets_synced:
            return
        with self._presets_lock:
            stored = [camera.presets[i] for i in sorted(camera.presets)]
        self._presets_synced.add(camera.mac)
        if stored:
            session.request_many(ptz.PRESET, [ptz.configure(p) for p in stored])

    def _maybe_auto_home(self, session: Session) -> None:
        """Save the first confirmed idle pose as `home` when none exists.

        Frigate's autotracking `return_preset` is usually `home`; with no such
        preset it fails every return. This never moves the camera: it records
        where the head already is, once, and only when enabled and no preset of
        that name exists. Operators replace it with "Save current view as home".
        """
        camera = session.camera
        if not self.auto_home or camera.mac in self._auto_home_checked or not camera.is_ptz:
            return
        if camera.motion.status != "IDLE":
            return
        self._auto_home_checked.add(camera.mac)
        with self._presets_lock:
            if any(is_home(p.name) for p in camera.presets.values()):
                return
            slot = self._free_slot(camera)
            if slot is None:
                return
            preset = Preset(index=slot, name="home", position=camera.motion.position)
            try:
                session.request(ptz.PRESET, ptz.configure(preset))
            except (OSError, ssl.SSLError):
                self._auto_home_checked.discard(camera.mac)
                return
            camera.presets[slot] = preset
            self._persist_presets(camera)
        log.warning(
            "no 'home' preset: saved the current position as home (slot %d) for %s; "
            "set ptz.auto_home=false to disable, or save a new home in the console",
            slot, camera.mac,
        )

    def poll_position(self, mac: str) -> bool:
        session = self._ptz_session(mac)
        if session is None:
            return False
        try:
            session.request_position(coalesce_sec=self.watchdog.probe_interval_sec)
        except (OSError, ssl.SSLError):
            return False
        return True

    def _control_session(self, mac: str) -> Session | None:
        """The management socket for a camera — where settings changes go (not PTZ)."""
        with self._sessions_lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.camera.mac == mac and not session.is_ptz_channel:
                return session
        return None

    def set_codec(self, mac: str, token: str, codec: Codec) -> bool:
        """Re-arm one channel with a new codec, live, via a fresh ChangeVideoSettings.

        The same path adoption uses. The choice is remembered (`track_codecs`) so a
        camera that reconnects comes back up on the codec last asked for.
        """
        session = self._control_session(mac)
        camera = self.cameras.get(mac)
        if session is None or camera is None or camera.track(token) is None:
            return False
        adoption.set_track_codecs(camera, {token: codec})
        self.track_codecs[token] = codec
        if token not in self.tracks:
            self.tracks.append(token)
        payload = adoption.video_settings(camera, self.ingest_host, self.ingest_port, [token])
        session.request(adoption.CHANGE_VIDEO, payload)
        log.info("re-armed %s as %s", token, codec.value)
        return True

    def snapshot(self, mac: str, timeout: float = 10.0) -> bytes | None:
        """Ask for a snapshot and wait for the camera to upload it to us.

        We do not fetch anything: the camera POSTs to the one-time URL we mint.
        """
        if self.images is None:
            return None
        session = self._session_for(mac)
        if session is None:
            return None
        token, url = self.images.mint(mac)
        session.request(adoption.GET_REQUEST, adoption.snapshot_request(url))
        return self.images.wait(token, timeout=timeout)

    def _session_for(self, mac: str) -> Session | None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.camera.mac == mac and not session.is_ptz_channel:
                return session
        return None

    def _ptz_session(self, mac: str) -> Session | None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.camera.mac == mac and session.is_ptz_channel:
                return session
        return None
