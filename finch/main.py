"""Run finch: be a G5 PTZ that isn't there.

    python3 main.py --host <controller> --user <user> --password <password>
    python3 main.py --host <controller> --token <adoption-token>

With credentials it mints its own adoption token; with `--token` it uses the one
you give it. Either way it dials the controller, says hello, and answers whatever
comes back until stopped.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Final

import adopt
import camera as camera_module
import gimbal as gimbal_module
import json
from unifiwire.envelope import Envelope
from unifiwire import certs
import identity
import ptzchannel
import push
import upload
import virtual_lens
from unifiwire import ws
from unifiwire import wsclient

DEFAULT_CERT: Final = "finch.pem"
RECONNECT_DELAY_SEC: Final = 5.0
IDLE_POLL_SEC: Final = 1.0
HEARTBEAT_SEC: Final = 10.0

log = logging.getLogger("finch")


@dataclass
class Options:
    host: str
    port: int = wsclient.DEFAULT_PORT
    token: str = ""
    username: str = ""
    password: str = ""
    cert: Path = Path(DEFAULT_CERT)
    mac: str = identity.DEFAULT_MAC
    name: str = identity.DEFAULT_NAME
    reconnect: bool = True
    source: Path | None = None
    fps: int = 30
    dump: Path | None = None
    virtual_lens: bool = False
    lens_state: Path | None = None


@dataclass
class Runner:
    """One camera, one connection at a time, reconnecting when dropped."""

    options: Options
    who: identity.Identity
    camera: camera_module.Camera
    connection: wsclient.Connection | None = None
    stopping: threading.Event = field(default_factory=threading.Event)
    pushers: dict[str, push.Pusher] = field(default_factory=dict)
    head: gimbal_module.Gimbal = field(default_factory=gimbal_module.Gimbal)
    ptz: ptzchannel.PtzChannel | None = None

    def stop(self) -> None:
        self.stopping.set()
        for track in list(self.pushers):
            self.stop_stream(track)
        if self.ptz is not None:
            self.ptz.stop()
            self.ptz = None
        if self.connection is not None:
            self.connection.close()

    def reconnect_ptz(self) -> None:
        """Exercise PTZ-channel recovery without resetting presets or lens state."""
        if self.ptz is not None:
            self.ptz.reconnect()

    # ------------------------------------------------------------------ streams

    def start_stream(self, stream: camera_module.Stream) -> None:
        """The controller armed a track. Dial its destination and start writing."""
        if self.options.source is None:
            log.warning("%s armed but no --source to send", stream.track)
            return
        where = stream.host_port
        channel = identity.channel(stream.track)
        if where is None or channel is None:
            log.warning("cannot push %s to %r", stream.track, stream.destination)
            return
        self.stop_stream(stream.track)
        source: push.VideoSource
        pace = True
        if self.options.virtual_lens:
            source = virtual_lens.LensSource(
                path=self.options.source,
                fps=self.options.fps,
                output_width=channel.width,
                output_height=channel.height,
                head=self.head,
                state_path=self.options.lens_state,
            )
            pace = False
        else:
            source = push.Source(self.options.source, fps=self.options.fps)
        pusher = push.Pusher(
            host=where[0],
            port=where[1],
            stream_name=stream.stream_name or stream.track,
            channel=channel,
            source=source,
            pace=pace,
        )
        self.pushers[stream.track] = pusher
        pusher.start()

    def stop_stream(self, track: str) -> None:
        pusher = self.pushers.pop(track, None)
        if pusher is not None:
            log.info("%s: stopping (%d tags sent)", track, pusher.tags_sent)
            pusher.stop()

    def label_ourselves(self) -> None:
        """Make it obvious in the controller's own UI that this camera is not real.

        Protect names an adopted camera after its model, so without this a virtual
        G5 PTZ appears beside a real one under the same name.
        """
        if not (self.options.username and self.options.password):
            return
        for _ in range(12):
            if self.stopping.wait(5.0):
                return
            if self.camera.adopted and adopt.rename(
                self.options.host, self.options.username, self.options.password,
                self.who.mac, self.options.name,
            ):
                return

    def record(self, direction: str, message: Envelope) -> None:
        """Write every message to a file. The cheapest way to answer "what did it send?"."""
        if self.options.dump is None:
            return
        line = json.dumps({
            "direction": direction,
            "functionName": message.function_name,
            "messageId": message.message_id,
            "inResponseTo": message.in_response_to,
            "payload": message.payload,
        })
        with self.options.dump.open("a") as handle:
            handle.write(line + "\n")

    def open_ptz(self, uri: str) -> None:
        """The controller handed us a URL; dial it back on the ptz1 subprotocol."""
        if self.ptz is not None:
            self.ptz.stop()
        self.ptz = ptzchannel.PtzChannel(
            uri=uri, who=self.who, certificate=self.options.cert, head=self.head
        )
        self.ptz.start()

    def send_snapshot(self, what: str, url: str) -> None:
        """The controller asked for a still; it wants it POSTed to that URL."""
        source = self.options.source
        image = upload.still_from(source) if source is not None else upload.PLACEHOLDER_JPEG
        threading.Thread(target=upload.send, args=(url, image), daemon=True).start()

    def token(self) -> str:
        if self.options.token:
            return self.options.token
        if not (self.options.username and self.options.password):
            raise adopt.AdoptionError("need either --token or --user and --password")
        management = adopt.fetch_token(
            self.options.host, self.options.username, self.options.password
        )
        return management.token

    def serve_forever(self) -> None:
        while not self.stopping.is_set():
            try:
                self.session()
            except (wsclient.HandshakeError, ConnectionError, OSError) as exc:
                log.warning("session ended: %s", exc)
            except adopt.AdoptionError as exc:
                log.error("%s", exc)
                return
            if not self.options.reconnect or self.stopping.is_set():
                return
            log.info("reconnecting in %.0fs", RECONNECT_DELAY_SEC)
            self.stopping.wait(RECONNECT_DELAY_SEC)

    def session(self) -> None:
        """One connection: dial, hello, then answer until it drops."""
        token = self.camera.token or self.token()
        self.camera.token = token
        log.info("dialling %s:%d as %s", self.options.host, self.options.port, self.who.mac)
        connection = wsclient.connect(
            host=self.options.host,
            port=self.options.port,
            mac=self.who.mac,
            certificate=self.options.cert,
            token=token,
            model=self.who.model_id,
            firmware=self.who.firmware,
            adopted=self.camera.adopted,
            camera_ip=self.who.ip,
            device_id=self.who.device_id,
            guid=self.who.guid,
        )
        self.connection = connection
        try:
            hello = self.camera.hello(self.options.host, self.options.port)
            connection.send(hello.to_json())
            log.info("hello sent, waiting for the controller")
            threading.Thread(target=self.label_ourselves, daemon=True).start()
            self.pump(connection)
        finally:
            connection.close()
            self.connection = None

    def pump(self, connection: wsclient.Connection) -> None:
        last_beat = time.time()
        while not self.stopping.is_set():
            if time.time() - last_beat >= HEARTBEAT_SEC:
                last_beat = time.time()
                beat = self.camera.time_sync()
                log.debug("-> %s id=%s", beat.function_name, beat.message_id)
                connection.send(beat.to_json())
            for frame in connection.receive(timeout=IDLE_POLL_SEC):
                if frame.opcode is ws.Opcode.PING:
                    log.debug("<- ping")
                    connection.pong(frame.payload)
                    continue
                if frame.opcode in (ws.Opcode.PONG, ws.Opcode.CONTINUATION):
                    continue
                if frame.opcode is ws.Opcode.CLOSE:
                    raise ConnectionError("controller closed the channel")
                for outgoing in self.camera.handle_bytes(frame.payload):
                    log.debug("-> %s id=%s", outgoing.function_name, outgoing.message_id)
                    connection.send(outgoing.to_json())


def build(options: Options) -> Runner:
    certs.ensure(options.cert, common_name=options.name)
    who = identity.Identity(mac=options.mac, name=options.name, ip=options.host)
    runner = Runner(options=options, who=who, camera=camera_module.Camera(who=who))
    runner.camera.on_stream_start = runner.start_stream
    runner.camera.on_stream_stop = runner.stop_stream
    runner.camera.on_snapshot = runner.send_snapshot
    runner.camera.on_traffic = runner.record
    runner.camera.on_ptz_requested = runner.open_ptz
    return runner


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pretend to be a UVC G5 PTZ.")
    parser.add_argument("--host", required=True, help="controller address")
    parser.add_argument("--port", type=int, default=wsclient.DEFAULT_PORT)
    parser.add_argument("--token", default="", help="adoption token, if you already have one")
    parser.add_argument("--user", default="", help="controller username, to mint a token")
    parser.add_argument("--password", default="", help="controller password, to mint a token")
    parser.add_argument("--cert", type=Path, default=Path(DEFAULT_CERT))
    parser.add_argument("--mac", default=identity.DEFAULT_MAC, help="our MAC, without separators")
    parser.add_argument("--name", default=identity.DEFAULT_NAME)
    parser.add_argument("--source", type=Path, default=None,
                        help="Annex B HEVC, or a local MP4 with --virtual-lens")
    parser.add_argument("--fps", type=int, default=30, help="rate to send the source at")
    parser.add_argument("--virtual-lens", action="store_true", help="render --source MP4 through the live PTZ viewport")
    parser.add_argument("--lens-state", type=Path, default=None, help="atomically publish virtual viewport JSON")
    parser.add_argument("--dump", type=Path, default=None,
                        help="write every message received to this file, one JSON object per line")
    parser.add_argument("--once", action="store_true", help="do not reconnect after a drop")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )
    runner = build(
        Options(
            host=args.host,
            port=args.port,
            token=args.token,
            username=args.user,
            password=args.password,
            cert=args.cert,
            mac=args.mac.replace(":", "").replace("-", "").upper(),
            name=args.name,
            reconnect=not args.once,
            source=args.source,
            fps=args.fps,
            dump=args.dump,
            virtual_lens=args.virtual_lens,
            lens_state=args.lens_state,
        )
    )

    def shutdown(signum: int, frame: FrameType | None) -> None:
        log.info("stopping")
        runner.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if hasattr(signal, "SIGHUP"):
        def reconnect_ptz(signum: int, frame: FrameType | None) -> None:
            log.info("forcing PTZ channel reconnect")
            runner.reconnect_ptz()

        signal.signal(signal.SIGHUP, reconnect_ptz)

    started = time.time()
    runner.serve_forever()
    log.info("finch stopped after %.0fs", time.time() - started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
