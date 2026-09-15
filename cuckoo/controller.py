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
from unifiwire import ws
from unifiwire import envelope
from unifiwire.envelope import CAMERA, HELLO, PARAM_AGREEMENT, TIME_SYNC, Envelope, Ids, decode
from model import Camera, Codec, Preset

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

log = logging.getLogger("cuckoo.control")


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
        ws.send(self.sock, message.to_json())

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
        try:
            while not self._stop.is_set():
                for key, _ in sel.select(timeout=SELECT_TIMEOUT_SEC):
                    if key.fileobj is listener:
                        self._spawn_accept(listener, context)
                    else:
                        assert isinstance(key.fileobj, socket.socket)
                        self._readable(key.fileobj, sel)
                self._drain_ready(sel)
                self._tick()
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
        camera = self.cameras.setdefault(mac, Camera(mac=mac))
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
            if session.camera.motion.open_channel():
                # A replacement PTZ socket invalidates any pending move from the
                # old one. Query immediately so ONVIF leaves UNKNOWN only after
                # the new channel has supplied truthful state.
                session.request(ptz.GET_POSITION, ptz.get_position())
        self._ready.put(session)

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

    def _drop(self, sock: socket.socket, sel: selectors.BaseSelector) -> None:
        recovery_control: Session | None = None
        with self._sessions_lock:
            session = self._sessions.pop(sock, None)
            if session is not None and session.is_ptz_channel:
                has_replacement = any(
                    candidate.camera.mac == session.camera.mac and candidate.is_ptz_channel
                    for candidate in self._sessions.values()
                )
                if not has_replacement:
                    session.camera.motion.disconnect()
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
        try:
            sel.unregister(sock)
        except (KeyError, ValueError):
            pass
        sock.close()

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
            self._on_frame(session, frame)

    def _on_frame(self, session: Session, frame: ws.Frame) -> None:
        if frame.opcode is ws.Opcode.CLOSE:
            session.sock.close()
            return
        if frame.opcode is ws.Opcode.PING:
            ws.send(session.sock, frame.payload, ws.Opcode.PONG)
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
                session.camera.motion.update(
                    position,
                    activity,
                    confirmed=name == ptz.GET_POSITION,
                    ignore_activity=bool(message.payload.get("ignoreActivity")),
                )
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
                log.info("PTZ channel restored: mac=%s", mac)
                del self._ptz_recovery[mac]
                continue
            session = controls.get(mac)
            if session is None:
                # PTZ recovery is meaningful only while its management channel is
                # live; a new management setup will enable it again.
                del self._ptz_recovery[mac]
                continue
            if recovery.exhausted:
                continue
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
                log.error(
                    "PTZ recovery timed out: mac=%s after %d attempts",
                    mac,
                    recovery.attempts,
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

    def _tick(self) -> None:
        now = time.time()
        for session in list(self._sessions.values()):
            if now - session.last_ping >= adoption.PING_INTERVAL_SEC:
                try:
                    ws.ping(session.sock)
                except OSError:
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
        self._recover_ptz(time.monotonic())


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

    def set_preset(self, mac: str, name: str, index: int | None = None) -> int | None:
        """Store the current position in a preset slot the camera keeps."""
        session = self._ptz_session(mac)
        camera = self.cameras.get(mac)
        if session is None or camera is None:
            return None
        slot = index if index is not None else camera.next_preset_index()
        if slot == ptz.SCRATCH_INDEX:
            slot += 1  # the scratch slot is overwritten by every arbitrary move
        preset = Preset(index=slot, name=name, position=camera.motion.position)
        session.request(ptz.PRESET, ptz.configure(preset))
        camera.presets[slot] = preset
        return slot

    def remove_preset(self, mac: str, index: int) -> bool:
        """Forget a preset.

        There is no measured delete action on this verb, so the slot is dropped
        from our own table and left to be overwritten rather than pretending to
        erase it on the camera.
        """
        camera = self.cameras.get(mac)
        if camera is None or index not in camera.presets:
            return False
        del camera.presets[index]
        return True

    def poll_position(self, mac: str) -> bool:
        session = self._ptz_session(mac)
        if session is None:
            return False
        session.request(ptz.GET_POSITION, ptz.get_position())
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
