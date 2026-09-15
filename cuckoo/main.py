"""Run cuckoo.

    python3 main.py --host 192.0.2.10

`--host` is the address the camera should reach us on; it is written into the
stream destinations, the snapshot upload URL and the PTZ callback, and it is what
ONVIF clients are told to come back to. It has to be routable from the camera and
from the client, so not a loopback.

Assembly lives in `build()` rather than inline, so the whole stack can be started
on ephemeral ports by a test.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from http import HTTPStatus
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Final

import config
from unifiwire import certs
import discovery
import events
import media
import mqtt_bridge
import onvif
import logbuffer
import rtsp
import snapshots
import setup_wizard
from controller import CONTROL_PORT, Controller, normalise_mac
from unifiwire.envelope import Envelope
from model import Camera, Codec, Position
from sentry_runtime import MovementGate
from frigate_config import validate_url

DEFAULT_CERT: Final = "cuckoo.pem"
SNAPSHOT_WAIT_SEC: Final = 10.0

log = logging.getLogger("cuckoo")


@dataclass
class Options:
    """Everything the stack needs to know. Ports are settable so tests can bind 0."""

    host: str
    bind_host: str = "0.0.0.0"
    cert: Path = Path(DEFAULT_CERT)
    name: str = "cuckoo"
    tracks: tuple[str, ...] = ("video1",)
    track_codecs: dict[str, Codec] = field(default_factory=dict)
    control_port: int = CONTROL_PORT
    ingest_port: int = media.INGEST_PORT
    snapshot_port: int = snapshots.SNAPSHOT_PORT
    rtsp_port: int = rtsp.RTSP_PORT
    onvif_port: int = onvif.ONVIF_PORT
    # Optional second ONVIF persona for read-only NVR/Protect consumption.
    # RTSP remains shared with the primary persona.
    onvif_read_only_port: int | None = None
    discovery_port: int = discovery.WS_DISCOVERY_PORT
    announce: bool = True
    dump: Path | None = None
    expected_camera_ip: str | None = None
    expected_camera_mac: str | None = None
    cameras: tuple[dict[str, object], ...] = ()
    mqtt: mqtt_bridge.MqttConfig = field(default_factory=mqtt_bridge.MqttConfig)
    config_path: Path = Path(config.DEFAULT_CONFIG_PATH)
    setup_mode: bool = False
    frigate_url: str | None = None


@dataclass
class Stack:
    """Every server, and the controller that drives the camera."""

    options: Options
    controller: Controller
    hub: media.Hub
    ingest: media.IngestServer
    images: snapshots.Store
    uploads: snapshots.SnapshotServer
    stream: rtsp.RtspServer
    services: onvif.Services
    north: onvif.OnvifServer
    finder: discovery.DiscoveryServer
    read_only_north: onvif.OnvifServer | None = None
    mqtt: mqtt_bridge.MqttBridge | None = None

    def start_servers(self) -> None:
        self.ingest.start()
        self.uploads.start()
        self.stream.start()
        self.north.start()
        if self.read_only_north is not None:
            self.read_only_north.start()
        self.finder.start()
        if self.mqtt is not None:
            self.mqtt.start()

    def stop(self) -> None:
        self.controller.stop()
        if self.mqtt is not None:
            self.mqtt.stop()
        for server in (self.finder, self.read_only_north, self.north, self.stream, self.uploads, self.ingest):
            if server is None:
                continue
            try:
                server.stop()
            except OSError as exc:  # pragma: no cover - shutdown races
                log.debug("stopping %s: %s", type(server).__name__, exc)

    def run(self) -> None:
        """Serve until stopped. The control channel owns this thread."""
        self.start_servers()
        self.controller.serve_forever()


def spec_to_tracks(spec: str) -> dict[str, str]:
    """Parse a `--tracks` value into `{channel: codec}`.

    Each entry is `name` or `name:codec`, e.g. `video1:h264,video2:h265`. A bare
    name defaults to h264 — the codec an ONVIF client needs — so `--tracks video1`
    arms one H.264 channel. This is the same shape as the config file's `tracks`
    object, so the two are interchangeable and the CLI simply replaces it.
    """
    out: dict[str, str] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if entry:
            name, _, codec = entry.partition(":")
            out[name] = codec or "h264"
    return out


def tracks_to_model(tracks: dict[str, str]) -> tuple[tuple[str, ...], dict[str, Codec]]:
    """Turn a `{channel: codec}` map into the arm order and typed codecs."""
    names: list[str] = []
    codecs: dict[str, Codec] = {}
    for name, codec in tracks.items():
        names.append(name)
        try:
            codecs[name] = Codec(codec)
        except ValueError:
            log.warning("unknown codec %r for track %s; using h264", codec, name)
            codecs[name] = Codec.H264
    return tuple(names), codecs


def resolve_options(args: argparse.Namespace) -> Options:
    """Merge config file, then CLI, into the Options the stack runs on.

    Precedence is defaults < config file < CLI flag. A flag left unset (None) does
    not override the file; a flag given always wins. A missing default config file
    is fine, but a `--config PATH` that does not exist is an error.
    """
    path = args.config or os.environ.get("OSPREY_CONFIG_FILE") or config.DEFAULT_CONFIG_PATH
    setup_wizard.load_environment(
        os.environ.get("OSPREY_SECRETS_FILE") or str(Path(path).parent / "osprey-secrets.env")
    )
    setup_mode = bool(getattr(args, "setup", False)) or os.environ.get("OSPREY_SETUP") == "1"
    if args.config and not os.path.exists(path) and not setup_mode:
        raise SystemExit(f"config file not found: {path}")
    effective = config.merged(config.load(path))
    config_had_host = bool(effective.get("host"))

    if args.host is not None:
        effective["host"] = args.host
    if getattr(args, "bind", None) is not None:
        effective["bind"] = args.bind
    elif os.environ.get("OSPREY_BIND"):
        effective["bind"] = os.environ["OSPREY_BIND"]
    if args.name is not None:
        effective["name"] = args.name
    if args.cert is not None:
        effective["cert"] = str(args.cert)
    if args.no_announce:
        effective["announce"] = False
    if args.tracks is not None:
        effective["tracks"] = spec_to_tracks(args.tracks)
    ports = effective["ports"]
    for flag, key in (
        (args.control_port, "control"), (args.ingest_port, "ingest"),
        (args.snapshot_port, "snapshot"), (args.rtsp_port, "rtsp"),
        (args.onvif_port, "onvif"),
        (getattr(args, "onvif_read_only_port", None), "onvif_read_only"),
        (args.discovery_port, "discovery"),
    ):
        if flag is not None:
            ports[key] = flag

    if not config_had_host and setup_mode:
        effective["host"] = args.host or os.environ.get("OSPREY_HOST") or "127.0.0.1"
    try:
        config.validate_runtime(effective)
    except ValueError as exc:
        raise SystemExit(f"invalid runtime configuration: {exc}") from exc
    if not effective["host"] and not setup_mode:
        raise SystemExit('no host set — pass --host or set "host" in the config file')
    try:
        registered = tuple(config.validate_cameras(effective.get("cameras", [])))
    except ValueError as exc:
        raise SystemExit(f"invalid camera registry: {exc}") from exc
    names, codecs = tracks_to_model(effective["tracks"])
    frigate_url: str | None = None
    frigate_config = effective.get("frigate")
    if isinstance(frigate_config, dict) and frigate_config.get("url") is not None:
        if not isinstance(frigate_config.get("url"), str):
            raise SystemExit('invalid runtime configuration: "frigate.url" must be a URL')
        try:
            frigate_url = validate_url(frigate_config["url"])
        except ValueError as exc:
            raise SystemExit(f"invalid runtime configuration: {exc}") from exc
    return Options(
        host=effective["host"],
        bind_host=str(effective.get("bind") or os.environ.get("OSPREY_BIND") or "0.0.0.0"),
        cert=Path(effective["cert"]),
        name=effective["name"],
        tracks=names,
        track_codecs=codecs,
        control_port=ports["control"],
        ingest_port=ports["ingest"],
        snapshot_port=ports["snapshot"],
        rtsp_port=ports["rtsp"],
        onvif_port=ports["onvif"],
        onvif_read_only_port=ports.get("onvif_read_only"),
        discovery_port=ports["discovery"],
        announce=effective["announce"],
        dump=args.dump,
        expected_camera_ip=getattr(args, "expected_camera_ip", None),
        expected_camera_mac=(
            normalise_mac(args.expected_camera_mac)
            if getattr(args, "expected_camera_mac", None)
            else None
        ),
        cameras=registered,
        mqtt=mqtt_bridge.MqttConfig.from_mapping(effective.get("mqtt")),
        config_path=Path(path),
        setup_mode=setup_mode and not config_had_host,
        frigate_url=frigate_url,
    )


def build(options: Options) -> Stack:
    certs.ensure(options.cert, common_name=options.name)

    hub = media.Hub()

    # Media and snapshot callbacks are separate unauthenticated TCP surfaces.
    # When camera addresses are known, keep them camera-only as well as guarding
    # the TLS control channel. An empty set preserves discovery for deployments
    # where the camera receives a dynamic address; those deployments must use a
    # firewall or VLAN boundary (see docs/security.md).
    allowed_camera_peers: set[str] = set()
    for item in options.cameras:
        camera_ip = item.get("ip")
        if isinstance(camera_ip, str) and camera_ip.strip():
            allowed_camera_peers.add(camera_ip.strip())
    if options.expected_camera_ip:
        allowed_camera_peers.add(options.expected_camera_ip)

    # Listeners first: every URL we hand out has to name the port actually bound,
    # not the one asked for. They differ whenever a port is left to the system.
    ingest = media.IngestServer(
        hub,
        port=options.ingest_port,
        fallback_name=options.tracks[0] if options.tracks else "video1",
        allowed_peers=allowed_camera_peers,
        bind_host=options.bind_host,
    )
    ingest_port = int(ingest.server_address[1])
    images = snapshots.Store("https://placeholder")  # rewritten below, once bound
    uploads = snapshots.SnapshotServer(
        images, cert=options.cert, port=options.snapshot_port,
        allowed_peers=allowed_camera_peers,
        bind_host=options.bind_host,
    )
    snapshot_port = int(uploads.server_address[1])
    images.base_url = f"https://{options.host}:{snapshot_port}"
    rtsp_user = os.environ.get("OSPREY_RTSP_USERNAME", "").strip()
    rtsp_password = os.environ.get("OSPREY_RTSP_PASSWORD", "")
    rtsp_password_file = os.environ.get("OSPREY_RTSP_PASSWORD_FILE", "")
    if rtsp_password_file:
        try:
            rtsp_password = Path(rtsp_password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read OSPREY_RTSP_PASSWORD_FILE: {exc}") from exc
    if bool(rtsp_user) != bool(rtsp_password):
        raise ValueError("OSPREY_RTSP_USERNAME and RTSP password must be configured together")
    rtsp_auth = rtsp.RtspAuth(rtsp_user, rtsp_password) if rtsp_user else None
    stream = rtsp.RtspServer(hub, advertise_host=options.host, port=options.rtsp_port, auth=rtsp_auth, bind_host=options.bind_host)

    control = Controller(
        cert=options.cert,
        ingest_host=options.host,
        control_port=options.control_port,
        ingest_port=ingest_port,
        tracks=list(options.tracks),
        track_codecs=options.track_codecs,
        name=options.name,
        hub=hub,
        images=images,
        expected_camera_ip=options.expected_camera_ip,
        expected_camera_mac=options.expected_camera_mac,
        configured_cameras={str(item["mac"]): item for item in options.cameras},
        bind_host=options.bind_host,
    )

    def current() -> Camera | None:
        adopted = [c for c in control.cameras.values() if c.adopted]
        if adopted:
            return adopted[0]
        return next(iter(control.cameras.values()), None)

    def mac_of() -> str | None:
        camera = current()
        return camera.mac if camera is not None else None

    def move(position: Position) -> bool:
        mac = mac_of()
        return control.move(mac, position) if mac else False

    def goto_preset(index: int, speed: int) -> bool:
        mac = mac_of()
        return control.goto_preset(mac, index, speed) if mac else False

    def set_preset(name: str, index: int | None) -> int | None:
        mac = mac_of()
        return control.set_preset(mac, name, index) if mac else None

    def remove_preset(index: int) -> bool:
        mac = mac_of()
        return control.remove_preset(mac, index) if mac else False

    def move_refusal() -> str:
        """Why a move cannot be dispatched right now, in the camera's own terms.

        A refusal is not the camera rejecting the command; it means the slot is
        not free to write on, and the three cases need different client reactions.
        """
        camera = current()
        if camera is None:
            return "no camera"
        if not camera.motion.available:
            return "PTZ channel unavailable"
        if camera.motion.activity != 0:
            return "the camera is already moving"
        return "the camera is not accepting movement"

    def refresh_position() -> bool:
        mac = mac_of()
        return control.poll_position(mac) if mac else False

    def set_encoder(token: str, codec: str) -> bool:
        mac = mac_of()
        if mac is None:
            return False
        try:
            return control.set_codec(mac, token, Codec(codec))
        except ValueError:
            return False

    def snapshot() -> bytes | None:
        """Ask for a fresh image; fall back to the last one if the camera is slow."""
        mac = mac_of()
        if mac is None:
            return None
        fresh = control.snapshot(mac, timeout=SNAPSHOT_WAIT_SEC)
        return fresh if fresh is not None else images.latest.get(mac)

    def backend_for(camera_mac: str | None) -> onvif.Backend:
        """Build a northbound backend pinned to one camera identity.

        The media and discovery listeners are shared, but every ONVIF service
        gets camera-specific PTZ, snapshot, and preset callables. This prevents
        a second adopted camera from accidentally moving the first one.
        """
        def selected() -> Camera | None:
            return control.cameras.get(camera_mac) if camera_mac else current()

        def selected_mac() -> str | None:
            camera = selected()
            return camera.mac if camera is not None else None

        def selected_move(position: Position) -> bool:
            mac = selected_mac()
            return control.move(mac, position) if mac else False

        def selected_goto(index: int, speed: int) -> bool:
            mac = selected_mac()
            return control.goto_preset(mac, index, speed) if mac else False

        def selected_set(name: str, index: int | None) -> int | None:
            mac = selected_mac()
            return control.set_preset(mac, name, index) if mac else None

        def selected_remove(index: int) -> bool:
            mac = selected_mac()
            return control.remove_preset(mac, index) if mac else False

        def selected_refresh() -> bool:
            mac = selected_mac()
            return control.poll_position(mac) if mac else False

        def selected_snapshot() -> bytes | None:
            mac = selected_mac()
            if mac is None:
                return None
            fresh = control.snapshot(mac, timeout=SNAPSHOT_WAIT_SEC)
            return fresh if fresh is not None else images.latest.get(mac)

        def selected_encoder(token: str, codec: str) -> bool:
            mac = selected_mac()
            if mac is None:
                return False
            try:
                return control.set_codec(mac, token, Codec(codec))
            except ValueError:
                return False

        def selected_refusal() -> str:
            camera = selected()
            if camera is None:
                return "no camera"
            if not camera.motion.available:
                return "PTZ channel unavailable"
            if camera.motion.activity != 0:
                return "the camera is already moving"
            return "the camera is not accepting movement"

        return onvif.Backend(
            camera=selected,
            stream_uri=lambda token: f"rtsp://{options.host}:{stream.port}/{token}",
            snapshot_uri=lambda token: f"http://{options.host}:{north.port}{onvif.SNAPSHOT_PATH}{token}",
            snapshot=selected_snapshot,
            move_absolute=selected_move,
            move_relative=selected_move,
            goto_preset=selected_goto,
            set_preset=selected_set,
            remove_preset=selected_remove,
            refresh_position=selected_refresh,
            move_refusal=selected_refusal,
            set_encoder=selected_encoder,
            telemetry=hub.stats,
        )

    backend = backend_for(None)
    registry = onvif.CameraRegistry()
    read_only_registry = onvif.CameraRegistry()
    setup = setup_wizard.SetupManager(
        options.config_path,
        secrets_path=os.environ.get("OSPREY_SECRETS_FILE"),
        frigate_path=os.environ.get("OSPREY_FRIGATE_CONFIG_FILE"),
    ) if options.setup_mode else None
    services = onvif.Services(
        backend, host=options.host, port=options.onvif_port,
        auth=onvif.AdminAuth.from_environment(),
        onvif_auth=onvif.OnvifAuth.from_environment(), setup=setup,
        frigate_url=options.frigate_url,
    )
    north = onvif.OnvifServer(services, port=options.onvif_port, registry=registry, bind_host=options.bind_host)
    # The service addresses it advertises must match where it is really listening.
    services.port = north.port
    if setup is not None:
        log.warning(
            "first-run setup pending; open http://%s:%d/setup and use setup token: %s",
            options.host, north.port, setup.token,
        )
    read_only_north: onvif.OnvifServer | None = None
    if options.onvif_read_only_port is not None:
        read_only_services = onvif.Services(
            backend,
            host=options.host,
            port=options.onvif_read_only_port,
            onvif_auth=services.onvif_auth,
            read_only=True,
        )
        read_only_north = onvif.OnvifServer(
            read_only_services, port=options.onvif_read_only_port,
            registry=read_only_registry,
            bind_host=options.bind_host,
        )
        read_only_services.port = read_only_north.port
    movement_gate = MovementGate()

    def mqtt_result(status: HTTPStatus, result: dict[str, object]) -> dict[str, object]:
        if status != HTTPStatus.OK:
            raise RuntimeError(str(result.get("error", "camera command refused")))
        return result

    def mqtt_step(data: dict[str, object]) -> object:
        with movement_gate.lease("mqtt"):
            status, result = services.control_step(data)
            return mqtt_result(status, result)

    def mqtt_zoom(data: dict[str, object]) -> object:
        with movement_gate.lease("mqtt"):
            status, result = services.control_zoom(data)
            return mqtt_result(status, result)

    def mqtt_goto(name: str) -> object:
        with movement_gate.lease("mqtt"):
            status, result = services.control_preset({"action": "goto", "name": name})
            return mqtt_result(status, result)

    def mqtt_home() -> object:
        with movement_gate.lease("mqtt"):
            status, result = services.control_home()
            return mqtt_result(status, result)

    mqtt_runtime: mqtt_bridge.MqttBridge | None = None
    if options.mqtt.enabled:
        mqtt_runtime = mqtt_bridge.MqttBridge(
            options.mqtt, mqtt_bridge.PahoMqttAdapter(options.mqtt), movement_gate,
            step=mqtt_step, zoom=mqtt_zoom, goto_preset=mqtt_goto, home=mqtt_home,
        )

    identity = discovery.identity_for(
        options.host, north.port, onvif.DEVICE_PATH, options.name, ptz=False
    )
    finder = discovery.DiscoveryServer(
        identity, port=options.discovery_port, multicast=options.announce
    )

    def adopted(camera: Camera) -> None:
        # Register a camera-pinned service as soon as adoption completes. The
        # primary unscoped service remains for backwards-compatible single-camera
        # clients; multi-camera clients use /cameras/{id}/ routes.
        if registry.get(onvif.canonical_camera_id(camera)) is None:
            camera_services = onvif.Services(
                backend_for(camera.mac), host=options.host, port=north.port,
                auth=services.auth, onvif_auth=services.onvif_auth,
            )
            registry.register(camera_services)
        if read_only_north is not None and read_only_registry.get(onvif.canonical_camera_id(camera)) is None:
            read_only_services = onvif.Services(
                backend_for(camera.mac), host=options.host, port=read_only_north.port,
                auth=services.auth, onvif_auth=services.onvif_auth, read_only=True,
            )
            read_only_registry.register(read_only_services)
        log.info(
            "camera ready: %s model=%s ptz=%s pan=%s..%s tilt=%s..%s",
            camera.mac, camera.model or "?", camera.is_ptz,
            camera.pan_range.minimum, camera.pan_range.maximum,
            camera.tilt_range.minimum, camera.tilt_range.maximum,
        )
        # Re-announce now that we know whether to claim PTZ.
        finder.identity = discovery.identity_for(
            options.host, north.port, onvif.DEVICE_PATH,
            camera.name or options.name, ptz=camera.is_ptz,
        )
        if options.announce:
            finder.say_hello()

    def detected(camera: Camera, detection: events.Detection) -> None:
        services.subscriptions.publish(
            onvif.detection_event(camera, detection.kind, detection.active)
        )

    def record(direction: str, message: Envelope) -> None:
        """Write every message to a file. The cheapest way to answer "what did we
        actually send it?" — which is most of the work with this protocol."""
        if options.dump is None:
            return
        with options.dump.open("a") as handle:
            handle.write(json.dumps({
                "direction": direction,
                "functionName": message.function_name,
                "messageId": message.message_id,
                "inResponseTo": message.in_response_to,
                "payload": message.payload,
            }) + "\n")

    control.on_adopted = adopted
    control.on_detection = detected
    control.on_traffic = record

    return Stack(
        options=options,
        controller=control,
        hub=hub,
        ingest=ingest,
        images=images,
        uploads=uploads,
        stream=stream,
        services=services,
        north=north,
        mqtt=mqtt_runtime,
        finder=finder,
        read_only_north=read_only_north,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Present a UniFi camera as an ONVIF device.")
    # Overridable settings default to None so the config file shows through when a
    # flag is omitted; a flag that is given always wins. See resolve_options().
    parser.add_argument("--config", default=None,
                        help=f"JSON config file (default {config.DEFAULT_CONFIG_PATH} if present)")
    parser.add_argument("--host", default=None, help="advertised address camera clients reach us on")
    parser.add_argument("--bind", default=None, help="local interface for HTTP/ONVIF listeners (default: 0.0.0.0)")
    parser.add_argument("--setup", action="store_true", help="start the browser first-run setup wizard")
    parser.add_argument("--control-port", type=int, default=None)
    parser.add_argument("--ingest-port", type=int, default=None)
    parser.add_argument("--snapshot-port", type=int, default=None)
    parser.add_argument("--rtsp-port", type=int, default=None)
    parser.add_argument("--onvif-port", type=int, default=None)
    parser.add_argument(
        "--onvif-read-only-port", type=int, default=None,
        help="optional second ONVIF port advertising media without PTZ",
    )
    parser.add_argument("--discovery-port", type=int, default=None)
    parser.add_argument("--cert", type=Path, default=None)
    parser.add_argument(
        "--tracks", default=None,
        help="comma-separated tracks to arm, each name[:codec] (bare name = h264). "
        "Overrides the config file's tracks. Default: h264 on video1/video2/video3.",
    )
    parser.add_argument("--name", default=None, help="controller name shown to the camera")
    parser.add_argument(
        "--no-announce", action="store_true", help="answer discovery probes but do not multicast"
    )
    parser.add_argument("--dump", type=Path, default=None,
                        help="write every message, in and out, as one JSON object per line")
    parser.add_argument(
        "--expected-camera-ip",
        default=None,
        help="reject camera control sockets from any other source IP",
    )
    parser.add_argument(
        "--expected-camera-mac",
        default=None,
        help="reject camera control sockets whose camera-mac header differs",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Configure the deployment's stderr/journal first, then attach the bounded
    # redacted tail used by the authenticated operator console.  ``basicConfig``
    # is a no-op when handlers already exist, so this order is intentional.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )
    logbuffer.install()

    options = resolve_options(args)
    stack = build(options)

    stopping = threading.Event()

    def shutdown(signum: int, frame: FrameType | None) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("stopping")
        stack.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info(
        "cuckoo up: control :%d, ingest :%d, snapshots :%d, rtsp :%d, onvif :%d%s",
        options.control_port, options.ingest_port, options.snapshot_port,
        options.rtsp_port, options.onvif_port, onvif.DEVICE_PATH,
    )
    stack.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
