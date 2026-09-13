"""The PTZ channel — the second socket, and the head on the end of it.

After adoption the controller sends `EnablePtzControl {"uri": …}`, and the camera
dials **back to that URI** with subprotocol `ptz1`. Motion commands then arrive on
that socket rather than the management one.

What arrives there, all measured:

    Preset {"action":"config","items":[{"index":1,"name":…,"pan":…,"tilt":…,
                                        "zoom":…,"focus":…}]}
    Preset {"action":"go","index":1,"speed":1000,"notifyCommandStatus":{}}
    GetCurrentPosition {"inDegree":true,"inSteps":true}

There is no absolute-move verb: an arbitrary move is *write a preset, then go to
it*. While travelling, the camera broadcasts `EventMotorState` — 80 of them for a
single move on the real device — and the final one lands exactly on the requested
position, which is what the controller treats as arrival.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import gimbal as gimbal_module
import identity
from unifiwire import ws
from unifiwire import wsclient
from unifiwire.envelope import CAMERA, CONTROLLER, Envelope, Ids, decode

PTZ_SUBPROTOCOL: Final = "ptz1"
PRESET: Final = "Preset"
GET_POSITION: Final = "GetCurrentPosition"
EVENT_MOTOR_STATE: Final = "EventMotorState"
CHANGE_AUTOTRACK: Final = "ChangePTZAutoTrackSettings"

BROADCAST_INTERVAL_SEC: Final = 0.05  # ~20/s while moving; the real head sends ~80 a move
SETTLED_BROADCASTS: Final = 2  # a couple after arrival, so the end position is unmissable
IDLE_POLL_SEC: Final = 0.2
RECONNECT_INITIAL_SEC: Final = 1.0
RECONNECT_MAX_SEC: Final = 5.0
CONNECT_TIMEOUT_SEC: Final = 5.0

log = logging.getLogger("finch.ptz")


@dataclass
class PtzChannel:
    """One `ptz1` connection: answers commands and reports the head's travel."""

    uri: str
    who: identity.Identity
    certificate: Path | None
    head: gimbal_module.Gimbal = field(default_factory=gimbal_module.Gimbal)
    ids: Ids = field(default_factory=lambda: Ids(start=20_000))
    stopping: threading.Event = field(default_factory=threading.Event)
    connection: wsclient.Connection | None = None
    broadcasts: int = 0
    _thread: threading.Thread | None = None

    # ------------------------------------------------------------------ plumbing

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.connection is not None:
            self.connection.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def reconnect(self) -> None:
        """Drop only the PTZ socket; the worker reconnects with head state intact."""
        connection = self.connection
        if connection is not None:
            connection.close()

    def _target(self) -> tuple[str, int]:
        parsed = urlparse(self.uri)
        host = parsed.hostname or ""
        port = parsed.port or wsclient.DEFAULT_PORT
        return host, port

    def _run(self) -> None:
        host, port = self._target()
        if not host:
            log.warning("cannot dial a PTZ channel at %r", self.uri)
            return
        delay = RECONNECT_INITIAL_SEC
        path = urlparse(self.uri).path or ws.CONTROL_PATH
        while not self.stopping.is_set():
            try:
                connection = wsclient.connect(
                    host=host,
                    port=port,
                    mac=self.who.mac,
                    certificate=self.certificate,
                    path=path,
                    subprotocol=PTZ_SUBPROTOCOL,
                    model=self.who.model_id,
                    firmware=self.who.firmware,
                    adopted=True,
                    camera_ip=self.who.ip,
                    device_id=self.who.device_id,
                    guid=self.who.guid,
                    timeout=CONNECT_TIMEOUT_SEC,
                )
            except (wsclient.HandshakeError, OSError) as exc:
                log.warning("PTZ channel refused: %s", exc)
            else:
                self.connection = connection
                log.info("ptz channel up to %s:%d", host, port)
                try:
                    self._pump(connection)
                except (ConnectionError, OSError) as exc:
                    log.info("ptz channel closed: %s", exc)
                finally:
                    connection.close()
                    self.connection = None

            if self.stopping.is_set():
                return
            log.info("reconnecting PTZ channel in %.0fs", delay)
            if self.stopping.wait(delay):
                return
            delay = min(delay * 2, RECONNECT_MAX_SEC)

    def _pump(self, connection: wsclient.Connection) -> None:
        settling = 0
        # A replacement socket must announce retained state even if the head
        # finished moving while the previous socket was unavailable.
        connection.send(self.broadcast().to_json())
        while not self.stopping.is_set():
            for frame in connection.receive(timeout=IDLE_POLL_SEC):
                if frame.opcode is ws.Opcode.PING:
                    connection.pong(frame.payload)
                    continue
                if frame.opcode is ws.Opcode.CLOSE:
                    raise ConnectionError("controller closed the ptz channel")
                if frame.opcode in (ws.Opcode.PONG, ws.Opcode.CONTINUATION):
                    continue
                for outgoing in self.handle_bytes(frame.payload):
                    connection.send(outgoing.to_json())

            # Travel, and say where we are while we do.
            if self.head.moving:
                self.head.step()
                connection.send(self.broadcast().to_json())
                settling = SETTLED_BROADCASTS
                time.sleep(BROADCAST_INTERVAL_SEC)
            elif settling > 0:
                settling -= 1
                connection.send(self.broadcast().to_json())
                log.info("arrived at %s", self.head.position())
                time.sleep(BROADCAST_INTERVAL_SEC)

    # ------------------------------------------------------------------ messages

    def message(self, name: str, payload: dict[str, Any]) -> Envelope:
        return Envelope(
            function_name=name,
            payload=payload,
            message_id=self.ids.next(),
            sender=CAMERA,
            recipient=CONTROLLER,
        )

    def reply(self, source: Envelope, payload: dict[str, Any]) -> Envelope:
        return Envelope(
            function_name=source.function_name,
            payload=payload,
            message_id=self.ids.next(),
            in_response_to=source.message_id,
            sender=CAMERA,
            recipient=CONTROLLER,
        )

    def broadcast(self) -> Envelope:
        self.broadcasts += 1
        return self.message(EVENT_MOTOR_STATE, self.head.motor_state())

    def handle_bytes(self, raw: bytes) -> list[Envelope]:
        try:
            message = decode(raw)
        except Exception as exc:
            log.debug("undecodable ptz frame: %s", exc)
            return []
        if message.sender == CAMERA:
            return []
        return self.handle(message)

    def handle(self, message: Envelope) -> list[Envelope]:
        name = message.function_name
        payload = message.payload
        log.debug("<- %s %s", name, payload.get("action", ""))

        if name == PRESET:
            return self._on_preset(message)
        if name == GET_POSITION:
            return [
                self.reply(
                    message,
                    self.head.current_position(
                        in_degree=bool(payload.get("inDegree", True)),
                        in_steps=bool(payload.get("inSteps", True)),
                    ),
                )
            ]
        if name == CHANGE_AUTOTRACK:
            return [self.reply(message, dict(payload))]
        return [self.reply(message, {})]

    def _on_preset(self, message: Envelope) -> list[Envelope]:
        """`config` writes a slot; `go` starts the head moving toward one."""
        payload = message.payload
        action = str(payload.get("action", ""))
        if action == "config":
            items = payload.get("items")
            if isinstance(items, list):
                self.head.configure([i for i in items if isinstance(i, dict)])
                log.info("preset %s configured", [i.get("index") for i in items if isinstance(i, dict)])
            return [self.reply(message, {})]
        if action == "go":
            index = payload.get("index")
            speed = payload.get("speed", gimbal_module.DEFAULT_SPEED)
            started = isinstance(index, int) and self.head.go(
                index, int(speed) if isinstance(speed, (int, float)) else gimbal_module.DEFAULT_SPEED
            )
            if started:
                log.info("going to preset %s at speed %s -> %s", index, speed, self.head.target)
            else:
                log.warning("asked to go to preset %r, which we were never given", index)
            return [self.reply(message, {})]
        return [self.reply(message, {})]
