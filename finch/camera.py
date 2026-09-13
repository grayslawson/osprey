"""The camera's side of the control channel.

finch dials the controller, says hello, and then spends its life answering. The
rules that matter, all learned from watching the real pair:

* the controller's hello reply is the gate — nothing else is expected before it
* every settings message wants an acknowledgement carrying `inResponseTo`, and
  the controller will not send the next one until it arrives
* `ChangeVideoSettings` is where streaming is turned on: the destinations in it
  are where video has to be pushed, and an empty list means stop
* `EnablePtzControl` asks us to dial a second socket with subprotocol `ptz1`

Nothing here does any I/O beyond the one socket, so the whole state machine can be
driven by a test with no network at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Final

import identity
import replies
from unifiwire import envelope as wire
from unifiwire.envelope import CAMERA, CONTROLLER, HELLO, PARAM_AGREEMENT, TIME_SYNC, Envelope, Ids, decode

CHANGE_VIDEO: Final = "ChangeVideoSettings"
CHANGE_ISP: Final = "ChangeIspSettings"
CHANGE_DEVICE: Final = "ChangeDeviceSettings"
CHANGE_OSD: Final = "ChangeOsdSettings"
CHANGE_SOUND_LED: Final = "ChangeSoundLedSettings"
CHANGE_SMART_DETECT: Final = "ChangeSmartDetectSettings"
CHANGE_AUDIO_EVENTS: Final = "ChangeAudioEventsSettings"
CHANGE_ANALYTICS: Final = "ChangeAnalyticsSettings"
NETWORK_STATUS: Final = "NetworkStatus"
GET_REQUEST: Final = "GetRequest"
START_SERVICE: Final = "StartService"
STOP_SERVICE: Final = "StopService"
UPDATE_FIRMWARE: Final = "UpdateFirmwareRequest"
REBOOT: Final = "Reboot"

ENABLE_PTZ: Final = "EnablePtzControl"
DISABLE_PTZ: Final = "DisablePtzControl"

log = logging.getLogger("finch.camera")


@dataclass
class Stream:
    """One track the controller has asked us to push, and where."""

    track: str
    destination: str
    stream_name: str = ""

    @property
    def host_port(self) -> tuple[str, int] | None:
        """Pull host and port out of a `tcp://host:port?query` destination."""
        text = self.destination
        if "://" in text:
            text = text.split("://", 1)[1]
        text = text.split("?", 1)[0]
        host, _, port = text.rpartition(":")
        if not host or not port.isdigit():
            return None
        return host, int(port)


@dataclass
class Camera:
    """Answers the controller. One instance per connection."""

    who: identity.Identity
    token: str = ""
    ids: Ids = field(default_factory=Ids)
    adopted: bool = False
    controller_name: str = ""
    controller_version: str = ""
    streams: dict[str, Stream] = field(default_factory=dict)
    ptz_uri: str = ""
    time_offset_ms: int = 0

    # Set by the runner: how to answer things the state machine cannot do itself.
    on_stream_start: Callable[[Stream], None] | None = None
    on_stream_stop: Callable[[str], None] | None = None
    on_snapshot: Callable[[str, str], None] | None = None
    on_ptz_requested: Callable[[str], None] | None = None
    # Every message in and out, for when the question is "what did it actually say?"
    on_traffic: Callable[[str, Envelope], None] | None = None

    # ------------------------------------------------------------------ sending

    def now(self) -> str:
        """Our clock, corrected by whatever the controller told us."""
        return wire.timestamp(time.time() + self.time_offset_ms / 1000)

    def message(self, name: str, payload: dict[str, Any]) -> Envelope:
        return Envelope(
            function_name=name,
            payload=payload,
            message_id=self.ids.next(),
            sender=CAMERA,
            recipient=CONTROLLER,
            at=self.now(),
        )

    def reply(self, source: Envelope, payload: dict[str, Any]) -> Envelope:
        return Envelope(
            function_name=source.function_name,
            payload=payload,
            message_id=self.ids.next(),
            in_response_to=source.message_id,
            sender=CAMERA,
            recipient=CONTROLLER,
            at=self.now(),
        )

    def hello(self, host: str, port: int = 7442) -> Envelope:
        return self.message(HELLO, self.who.hello(self.token, host, port))

    def time_sync(self) -> Envelope:
        """The camera's own heartbeat: "how wrong is my clock?".

        It is the camera that asks, not the controller, and a channel that goes
        entirely silent is treated as stale further up the stack — so this doubles
        as proof of life.
        """
        return self.message(TIME_SYNC, {"timeDelta": self.time_offset_ms})

    # ---------------------------------------------------------------- receiving

    def handle_bytes(self, raw: bytes) -> list[Envelope]:
        try:
            message = decode(raw)
        except Exception as exc:  # a malformed frame must not end the session
            log.debug("undecodable frame: %s", exc)
            return []
        if message.sender == CAMERA:
            return []  # our own echo
        return self.handle(message)

    def handle(self, message: Envelope) -> list[Envelope]:
        """Answer one controller message. Returns what to send back, in order."""
        name = message.function_name
        payload = message.payload
        log.debug("<- %s id=%s", name, message.message_id)
        if self.on_traffic is not None:
            self.on_traffic("in", message)

        if name == HELLO:
            return self._on_hello(message)
        if name == PARAM_AGREEMENT:
            return [self.reply(message, self.who.param_agreement(self.token))]
        if name == TIME_SYNC:
            # Our own heartbeat comes back as a reply; answering it would loop.
            if message.is_reply:
                self._on_time_sync(payload)
                return []
            return [self.reply(message, self._on_time_sync(payload))]
        if name == CHANGE_VIDEO:
            return [self.reply(message, self._on_video(payload))]
        if name == CHANGE_ISP:
            return [self.reply(message, replies.isp_settings(payload))]
        if name == CHANGE_DEVICE:
            return [self.reply(message, replies.device_settings(payload, self.who.name))]
        if name == CHANGE_OSD:
            return [self.reply(message, replies.osd_settings(payload))]
        if name == CHANGE_SOUND_LED:
            return [self.reply(message, replies.sound_led_settings(payload))]
        if name == NETWORK_STATUS:
            return [self.reply(message, replies.network_status())]
        if name == CHANGE_ANALYTICS:
            return [self.reply(message, replies.analytics_settings(payload))]
        if name == GET_REQUEST:
            return self._on_get_request(message)
        if name == ENABLE_PTZ:
            return self._on_enable_ptz(message)
        if name == DISABLE_PTZ:
            self.ptz_uri = ""
            return [self.reply(message, {})]
        if name in (UPDATE_FIRMWARE, REBOOT):
            # Never act on these. Acknowledge so the controller is not left waiting.
            log.info("declining %s", name)
            return [self.reply(message, {})]
        if name in (START_SERVICE, STOP_SERVICE):
            log.info("%s %s", name, payload.get("service"))
            return [self.reply(message, dict(payload))]

        return [self.reply(message, replies.generic_echo(payload))]

    # ------------------------------------------------------------------ handlers

    def _on_hello(self, message: Envelope) -> list[Envelope]:
        """The controller's reply to our hello. Adoption is done at this point."""
        payload = message.payload
        self.controller_name = str(payload.get("controllerName", ""))
        self.controller_version = str(payload.get("controllerVersion", ""))
        self.adopted = True
        log.info(
            "adopted by %s %s", self.controller_name or "?", self.controller_version or "?"
        )
        return []  # a reply to our own message needs no answer

    def _on_time_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The controller answers with its clock; take the offset rather than the time."""
        remote = payload.get("t1") or payload.get("t2")
        if isinstance(remote, (int, float)):
            self.time_offset_ms = int(remote) - int(time.time() * 1000)
        return {"timeDelta": self.time_offset_ms}

    def _on_video(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Arm or disarm tracks. This is the only place streaming is turned on."""
        asked = payload.get("video")
        asked = asked if isinstance(asked, dict) else {}
        armed: dict[str, str] = {}
        for track, settings in asked.items():
            if not isinstance(settings, dict):
                continue
            serializer = settings.get("avSerializer")
            serializer = serializer if isinstance(serializer, dict) else {}
            destinations = serializer.get("destinations")
            parameters = serializer.get("parameters")
            parameters = parameters if isinstance(parameters, dict) else {}
            if isinstance(destinations, list) and destinations:
                stream = Stream(
                    track=track,
                    destination=str(destinations[0]),
                    stream_name=str(parameters.get("streamName", track)),
                )
                self.streams[track] = stream
                armed[track] = stream.destination
                log.info("armed %s -> %s", track, stream.destination)
                if self.on_stream_start is not None:
                    self.on_stream_start(stream)
            elif isinstance(destinations, list):
                if self.streams.pop(track, None) is not None:
                    log.info("disarmed %s", track)
                    if self.on_stream_stop is not None:
                        self.on_stream_stop(track)
        return replies.video_settings(payload, armed)

    def _on_get_request(self, message: Envelope) -> list[Envelope]:
        """A snapshot request: an upload URL we are expected to POST an image to."""
        payload = message.payload
        what = str(payload.get("what", ""))
        uri = str(payload.get("uri", ""))
        if what == "snapshot" and uri and self.on_snapshot is not None:
            self.on_snapshot(what, uri)
        return [self.reply(message, {})]

    def _on_enable_ptz(self, message: Envelope) -> list[Envelope]:
        """The controller hands us a URL to dial back on the ptz1 subprotocol."""
        self.ptz_uri = str(message.payload.get("uri", ""))
        log.info("ptz channel requested at %s", self.ptz_uri or "?")
        if self.on_ptz_requested is not None and self.ptz_uri:
            self.on_ptz_requested(self.ptz_uri)
        return [self.reply(message, {})]
