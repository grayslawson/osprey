"""Run osprey.

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
import re
import signal
import sys
import threading
import time
from http import HTTPStatus
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, Final

import arbitration
import config
import ptz
from unifiwire import certs
import discovery
import events
import identity
import media
import mqtt_bridge
import onvif
import logbuffer
import logfile
import rtsp
import snapshots
import secure_defaults
import setup_wizard
from version import get_version
from controller import CONTROL_PORT, Controller, PtzWatchdog, normalise_mac
from presets import PresetStore
from unifiwire.envelope import Envelope
from model import Camera, Codec, Position
from sentry_manager import SentryManager, SentryStore
from sentry_runtime import MovementGate, SentryConfig
from frigate_config import validate_url

DEFAULT_CERT: Final = "osprey.pem"
SNAPSHOT_WAIT_SEC: Final = 10.0
# The marker that names the media-only view of a camera: the second ONVIF
# device an NVR such as UniFi Protect adopts while the tracker keeps the head.
MEDIA_PERSONA: Final = "media"

log = logging.getLogger("osprey")


@dataclass
class Options:
    """Everything the stack needs to know. Ports are settable so tests can bind 0."""

    host: str
    bind_host: str = "0.0.0.0"
    cert: Path = Path(DEFAULT_CERT)
    name: str = "osprey"
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
    ptz_max_speed: int = ptz.DEFAULT_MAX_SPEED
    announce: bool = True
    dump: Path | None = None
    expected_camera_ip: str | None = None
    expected_camera_mac: str | None = None
    cameras: tuple[dict[str, object], ...] = ()
    mqtt: mqtt_bridge.MqttConfig = field(default_factory=mqtt_bridge.MqttConfig)
    config_path: Path = Path(config.DEFAULT_CONFIG_PATH)
    setup_mode: bool = False
    frigate_url: str | None = None
    ptz: dict[str, Any] = field(default_factory=lambda: config.validate_ptz(None))
    sentry: dict[str, Any] = field(default_factory=lambda: config.validate_sentry(None))
    onvif_identity: identity.IdentityConfig = field(default_factory=identity.IdentityConfig)
    onvif_log_calls: bool = False
    discovery_interface: str | None = None
    discovery_address: str | None = None
    hello_interval: float = 300.0
    discovery_scopes: tuple[str, ...] = ()
    # "legacy" keeps the old open defaults; resolve_options sets "secure" for a new install.
    security: str = "legacy"


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
    sentry: SentryManager | None = None

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
        if self.sentry is not None:
            self.sentry.start()

    def stop(self) -> None:
        self.controller.stop()
        if self.sentry is not None:
            self.sentry.stop()
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
    path = (args.config or os.environ.get("OSPREY_CONFIG_FILE")
            or config.with_legacy(config.DEFAULT_CONFIG_PATH, config.LEGACY_CONFIG_PATH))
    setup_wizard.load_environment(
        os.environ.get("OSPREY_SECRETS_FILE") or str(Path(path).parent / "osprey-secrets.env")
    )
    setup_mode = bool(getattr(args, "setup", False)) or os.environ.get("OSPREY_SETUP") == "1"
    if args.config and not os.path.exists(path) and not setup_mode:
        raise SystemExit(f"config file not found: {path}")
    try:
        effective = config.merged(config.load(path))
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        raise SystemExit(f"invalid config file {path}: {exc}") from exc
    config_had_host = bool(effective.get("host"))

    if args.host is not None:
        effective["host"] = args.host
    if getattr(args, "bind", None) is not None:
        effective["bind"] = args.bind
    elif os.environ.get("OSPREY_BIND"):
        effective["bind"] = os.environ["OSPREY_BIND"]
    if os.environ.get("OSPREY_PTZ_MAX_SPEED"):
        try:
            effective.setdefault("ptz", {})["max_speed"] = int(os.environ["OSPREY_PTZ_MAX_SPEED"])
        except ValueError:
            raise SystemExit("invalid runtime configuration: OSPREY_PTZ_MAX_SPEED must be an integer") from None
    if args.name is not None:
        effective["name"] = args.name
    if args.cert is not None:
        effective["cert"] = str(args.cert)
    elif effective["cert"] == config.DEFAULTS["cert"]:
        # The camera trusts the existing certificate; never mint a new one just for the rename.
        effective["cert"] = config.with_legacy(effective["cert"], config.LEGACY_CERT_PATH)
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
    try:
        config.validate_onvif(effective.get("onvif", {}), list(registered))
    except ValueError as exc:
        raise SystemExit(f"invalid runtime configuration: {exc}") from exc
    onvif_settings = effective.get("onvif") or {}
    discovery_settings = effective.get("discovery") or {}
    names, codecs = tracks_to_model(effective["tracks"])
    try:
        ptz_settings = config.validate_ptz(effective.get("ptz"))
    except ValueError as exc:
        raise SystemExit(f"invalid runtime configuration: {exc}") from exc
    try:
        sentry_settings = config.validate_sentry(effective.get("sentry"))
    except ValueError as exc:
        raise SystemExit(f"invalid runtime configuration: {exc}") from exc
    frigate_url: str | None = None
    frigate_config = effective.get("frigate")
    if isinstance(frigate_config, dict) and frigate_config.get("url") is not None:
        if not isinstance(frigate_config.get("url"), str):
            raise SystemExit('invalid runtime configuration: "frigate.url" must be a URL')
        try:
            frigate_url = validate_url(frigate_config["url"])
        except ValueError as exc:
            raise SystemExit(f"invalid runtime configuration: {exc}") from exc
    try:
        mqtt_config = mqtt_bridge.MqttConfig.from_mapping(effective.get("mqtt"))
        hello_interval = float(discovery_settings.get("hello_interval", 300))
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"invalid runtime configuration: {exc}") from exc
    cert_path = Path(effective["cert"])
    return Options(
        security=secure_defaults.resolve(cert_path.parent, (cert_path, Path(path))),
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
        ptz_max_speed=int(effective["ptz"]["max_speed"]),
        announce=effective["announce"],
        dump=args.dump,
        expected_camera_ip=getattr(args, "expected_camera_ip", None),
        expected_camera_mac=(
            normalise_mac(args.expected_camera_mac)
            if getattr(args, "expected_camera_mac", None)
            else None
        ),
        cameras=registered,
        mqtt=mqtt_config,
        config_path=Path(path),
        setup_mode=setup_mode and not config_had_host,
        frigate_url=frigate_url,
        ptz=ptz_settings,
        sentry=sentry_settings,
        onvif_identity=identity.IdentityConfig.from_mapping(onvif_settings.get("identity")),
        onvif_log_calls=bool(onvif_settings.get("log_calls", False)),
        discovery_interface=discovery_settings.get("interface"),
        discovery_address=discovery_settings.get("address"),
        hello_interval=hello_interval,
        discovery_scopes=tuple(discovery_settings.get("scopes") or ()),
    )


def preset_path(options: Options) -> Path:
    """Where named PTZ positions persist: env, then config, then the state dir.

    The certificate is always generated into a writable state directory, so
    its parent is the natural default on every supported install path.
    """
    explicit = os.environ.get("OSPREY_PRESETS_FILE") or options.ptz.get("presets_file")
    if isinstance(explicit, str) and explicit.strip():
        return Path(explicit)
    return options.cert.parent / "osprey-presets.json"


class RateLimitedWarning:
    """Log the same operator-relevant warning at most once per interval."""

    def __init__(self, logger: logging.Logger, interval: float = 30.0) -> None:
        self.logger, self.interval = logger, interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def __call__(self, message: str, *args: object) -> None:
        now = time.monotonic()
        key = message % args if args else message
        with self._lock:
            if now - self._last.get(key, -self.interval) < self.interval:
                return
            self._last[key] = now
        self.logger.warning(message, *args)


def build(options: Options) -> Stack:
    certs.ensure(options.cert, common_name=options.name)

    hub = media.Hub()

    # Media and snapshot callbacks are separate unauthenticated TCP surfaces.
    # When camera addresses are known, keep them camera-only as well as guarding
    # the TLS control channel. An empty set preserves discovery for deployments
    # where the camera receives a dynamic address; those deployments must use a
    # firewall or VLAN boundary (see docs/security.md).
    secure = options.security == "secure"
    allowed_camera_peers: set[str] = set()
    for item in options.cameras:
        camera_ip = item.get("ip")
        if isinstance(camera_ip, str) and camera_ip.strip():
            allowed_camera_peers.add(camera_ip.strip())
    if options.expected_camera_ip:
        allowed_camera_peers.add(options.expected_camera_ip)

    # Secure installs enforce the list even while empty: the first camera that
    # completes the control handshake adds its IP (controller.learned_peers).
    # Legacy installs without a configured IP stay open, as before.
    state_dir = options.cert.parent
    learn = secure and not allowed_camera_peers
    camera_peers: set[str] | None = allowed_camera_peers if (allowed_camera_peers or secure) else None
    if camera_peers is None:
        log.warning("media and snapshot ports accept any LAN host; set cameras[].ip or --expected-camera-ip")

    # Listeners first: every URL we hand out has to name the port actually bound,
    # not the one asked for. They differ whenever a port is left to the system.
    ingest = media.IngestServer(
        hub,
        port=options.ingest_port,
        fallback_name=options.tracks[0] if options.tracks else "video1",
        allowed_peers=camera_peers,
        bind_host=options.bind_host,
    )
    ingest_port = int(ingest.server_address[1])
    images = snapshots.Store("https://placeholder")  # rewritten below, once bound
    uploads = snapshots.SnapshotServer(
        images, cert=options.cert, port=options.snapshot_port,
        allowed_peers=camera_peers,
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
    # One credential store for ONVIF, snapshots and (opt-in) RTSP.
    onvif_auth = onvif.OnvifAuth.from_environment()
    if secure and not onvif_auth.configured and os.environ.get("OSPREY_ONVIF_AUTH") != "disabled":
        onvif_auth = onvif.OnvifAuth(
            secure_defaults.USERNAME, secure_defaults.credential(state_dir, "onvif"),
            users=onvif_auth.directory,
        )
    if secure and not rtsp_user and not onvif_auth.directory.rtsp_require_auth \
            and os.environ.get("OSPREY_RTSP_AUTH") != "disabled":
        rtsp_user = secure_defaults.USERNAME
        rtsp_password = secure_defaults.credential(state_dir, "rtsp")
    if not secure and not onvif_auth.configured:
        log.warning("ONVIF is anonymous: anyone on the network can move the camera; enable auth (docs/security.md)")
    if not secure and not rtsp_user and not onvif_auth.directory.rtsp_require_auth:
        log.warning("RTSP is anonymous: anyone on the network can watch the camera")
    directory = onvif_auth.directory
    rtsp_auth: rtsp.RtspAuth | None = None
    if rtsp_user or directory.rtsp_require_auth:
        rtsp_auth = rtsp.RtspAuth(
            rtsp_user or None, rtsp_password or None,
            users=directory.users if directory.rtsp_require_auth else (),
            anonymous_from=directory.rtsp_anonymous_from,
        )
        if not rtsp_auth.users:
            raise ValueError("RTSP authentication is required but no user holds the media role")
    stream = rtsp.RtspServer(hub, advertise_host=options.host, port=options.rtsp_port, auth=rtsp_auth, bind_host=options.bind_host, known_tracks=options.tracks)

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
        learned_peers=camera_peers if learn else None,
        preset_store=PresetStore(preset_path(options)),
        isp_baseline=options.ptz.get("isp_baseline"),
        auto_home=bool(options.ptz.get("auto_home", True)),
        watchdog=PtzWatchdog(
            reply_timeout_sec=float(options.ptz.get("reply_timeout_sec", 30.0)),
            min_silence_sec=float(options.ptz.get("min_silence_sec", 10.0)),
            max_unanswered=int(options.ptz.get("max_unanswered", 3)),
            probe_interval_sec=float(options.ptz.get("probe_interval_sec", 2.0)),
            idle_probe_sec=float(options.ptz.get("idle_probe_sec", 60.0)),
        ),
        arbiter=arbitration.Arbiter(float(options.ptz.get("manual_hold_sec", arbitration.DEFAULT_HOLD_SEC))),
    )
    refusal_warning = RateLimitedWarning(log)

    # One movement gate for every PTZ producer: the operator console, MQTT,
    # autotracking and the idle patrol. Sharing it is what keeps a patrol from
    # moving the camera while a person is moving it.
    movement_gate = MovementGate()

    def sentry_preset_slot(mac: str, name: str) -> int | None:
        """The slot a named preset occupies, matched case-insensitively."""
        camera = control.cameras.get(mac)
        if camera is None:
            return None
        wanted = name.strip().casefold()
        for slot, preset in camera.presets.items():
            if preset.name.strip().casefold() == wanted:
                return slot
        return None

    def sentry_goto(mac: str, name: str, speed: int) -> bool:
        """Move the camera to a named preset on behalf of a patrol."""
        slot = sentry_preset_slot(mac, name)
        if slot is None:
            log.warning("sentry: camera %s has no preset named %r", mac, name)
            return False
        moved = control.goto_preset(mac, slot, speed, strict=True)
        if not moved:
            # A refusal because a person is in control, or because the head is
            # still moving, is contention rather than failure: defer the move
            # without consuming a point of the tour.
            if control.arbiter.remaining(mac) > 0:
                raise BlockingIOError("a person is in control")
            camera = control.cameras.get(mac)
            if camera is not None and camera.motion.activity != 0:
                raise BlockingIOError("the camera is still moving")
        return moved

    def sentry_tracking(mac: str, hold_sec: float) -> bool:
        """True while an autotracker is driving this camera."""
        return control.auto_active(mac, hold_sec)

    def sentry_idle(mac: str) -> bool:
        """True when the camera is adopted, has PTZ, and has settled."""
        camera = control.cameras.get(mac)
        if camera is None or not camera.adopted or not camera.motion.available:
            return False
        return camera.motion.activity == 0

    def sentry_presets(mac: str) -> list[str]:
        camera = control.cameras.get(mac)
        if camera is None:
            return []
        return sorted(preset.name for preset in camera.presets.values())

    sentry_manager = SentryManager(
        goto_preset=sentry_goto,
        tracking_active=sentry_tracking,
        camera_idle=sentry_idle,
        gate=movement_gate,
        preset_names=sentry_presets,
        store=SentryStore(options.cert.parent / "sentry.json"),
    )
    # The config file is the baseline; the state file holds whatever an
    # operator changed at runtime, so a restart does not drop a running patrol.
    sentry_manager.restore(dict(options.sentry.get("cameras") or {}))

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

        def selected_move(position: Position, speed: int) -> bool:
            mac = selected_mac()
            return control.move(mac, position, speed) if mac else False

        def selected_move_continuous(target: Position, delta: Position, speed: int) -> bool:
            mac = selected_mac()
            return control.move_continuous(mac, target, delta, speed) if mac else False

        def selected_goto(index: int, speed: int) -> bool:
            mac = selected_mac()
            return control.goto_preset(mac, index, speed) if mac else False

        def selected_set(name: str, index: int | None) -> int | None:
            mac = selected_mac()
            return control.set_preset(mac, name, index) if mac else None

        def selected_change_isp(settings: dict[str, Any]) -> bool | None:
            mac = selected_mac()
            return control.change_isp(mac, settings) if mac else False

        def selected_remove(index: int) -> bool:
            mac = selected_mac()
            return control.remove_preset(mac, index) if mac else False

        def selected_refresh() -> bool:
            mac = selected_mac()
            return control.poll_position(mac) if mac else False

        def selected_stop(pan_tilt: bool, zoom: bool) -> bool:
            mac = selected_mac()
            return control.stop_motion(mac, pan_tilt, zoom) if mac else False

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
                reason = "no camera"
            elif not camera.motion.available:
                reason = "PTZ channel unavailable"
            elif camera.motion.activity != 0:
                reason = "the camera is already moving"
            elif camera.motion.error is not None:
                reason = f"PTZ state unknown: {camera.motion.error}"
            else:
                reason = "the camera is not accepting movement"
            refusal_warning("PTZ move refused: %s", reason)
            return reason

        def selected_recover() -> bool:
            mac = selected_mac()
            return control.recover_ptz(mac) if mac else False

        def selected_health() -> dict[str, object]:
            mac = selected_mac()
            return control.ptz_health(mac) if mac else {
                "ptz": False, "healthy": False, "state": "UNKNOWN", "error": "no camera",
            }

        return onvif.Backend(
            camera=selected,
            stream_uri=lambda token: f"rtsp://{options.host}:{stream.port}/{token}",
            snapshot_uri=lambda token: f"http://{options.host}:{north.port}{onvif.SNAPSHOT_PATH}{token}",
            snapshot=selected_snapshot,
            move_absolute=selected_move,
            move_relative=selected_move,
            move_continuous=selected_move_continuous,
            goto_preset=selected_goto,
            set_preset=selected_set,
            remove_preset=selected_remove,
            change_isp=selected_change_isp,
            refresh_position=selected_refresh,
            stop=selected_stop,
            move_refusal=selected_refusal,
            set_encoder=selected_encoder,
            recover_ptz=selected_recover,
            ptz_health=selected_health,
            camera_connected=lambda: (mac := selected_mac()) is not None and control.is_connected(mac),
            reboot_counters=lambda: {
                "requests": control.reboot_requests_total,
                "suppressed": control.reboot_suppressed_total,
            },
            telemetry=lambda: rtsp.telemetry(hub, stream),
            max_speed=options.ptz_max_speed,
            sentry=lambda: sentry_manager,
        )

    backend = backend_for(None)
    registry = onvif.CameraRegistry()
    read_only_registry = onvif.CameraRegistry()
    setup = setup_wizard.SetupManager(
        options.config_path,
        secrets_path=os.environ.get("OSPREY_SECRETS_FILE"),
        frigate_path=os.environ.get("OSPREY_FRIGATE_CONFIG_FILE"),
    ) if options.setup_mode else None
    interface_hw = (
        identity.interface_mac(options.discovery_interface) if options.discovery_interface else None
    )
    northbound = {
        "identity": options.onvif_identity,
        "name": options.name,
        "interface_mac": interface_hw,
        "extra_scopes": options.discovery_scopes,
        "autotrack_users": frozenset(options.ptz["autotrack_users"]),
        "log_calls": bool(options.onvif_log_calls),
    }
    services = onvif.Services(
        backend, host=options.host, port=options.onvif_port,
        auth=onvif.AdminAuth.from_environment(),
        onvif_auth=onvif_auth, setup=setup,
        frigate_url=options.frigate_url,
        identity=options.onvif_identity, name=options.name,
        interface_mac=interface_hw, extra_scopes=options.discovery_scopes,
        autotrack_users=frozenset(options.ptz["autotrack_users"]),
        log_calls=bool(options.onvif_log_calls),
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
            persona=MEDIA_PERSONA,
            **northbound,  # type: ignore[arg-type]
        )
        read_only_north = onvif.OnvifServer(
            read_only_services, port=options.onvif_read_only_port,
            registry=read_only_registry,
            bind_host=options.bind_host,
        )
        read_only_services.port = read_only_north.port
        log.warning(
            "read-only ONVIF persona on :%d for an NVR such as UniFi Protect. If Protect runs on "
            "this network, block the physical camera from it first: `osprey protect-guard` "
            "(docs/protect-guard.md)", read_only_north.port,
        )

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

    def sentry_resolve(value: object) -> str:
        """Resolve a camera reference — a MAC or a display name — to a MAC."""
        wanted = str(value).strip() if value is not None else ""
        if not wanted:
            camera = current()
            if camera is None:
                raise ValueError("no camera is available")
            return camera.mac
        normalised = wanted.replace(":", "").replace("-", "").upper()
        if re.fullmatch(r"[0-9A-F]{12}", normalised) and normalised in control.cameras:
            return normalised
        for camera in control.cameras.values():
            if camera.name.strip().casefold() == wanted.casefold():
                return camera.mac
        raise ValueError(f"unknown camera {wanted!r}")

    def mqtt_sentry(data: dict[str, object]) -> object:
        """Configure one camera's idle patrol."""
        mac = sentry_resolve(data.get("camera"))
        existing = sentry_manager.config(mac)
        base: dict[str, object] = existing.as_dict() if existing is not None else {
            "enabled": False,
            "interval_sec": float(options.sentry.get("interval_sec", 45)),
            "speed": int(options.sentry.get("speed", ptz.DEFAULT_SPEED)),
            "tracking_hold_sec": float(options.sentry.get("tracking_hold_sec", 15)),
            "points": [],
        }
        merged = {**base, **{key: value for key, value in data.items() if key != "camera"}}
        sentry_config = SentryConfig.from_mapping(merged)
        sentry_manager.configure(mac, sentry_config)
        return {"ok": True, "camera": mac, "sentry": sentry_manager.snapshot(mac)}

    mqtt_runtime: mqtt_bridge.MqttBridge | None = None
    if options.mqtt.enabled:
        mqtt_runtime = mqtt_bridge.MqttBridge(
            options.mqtt, mqtt_bridge.PahoMqttAdapter(options.mqtt), movement_gate,
            step=mqtt_step, zoom=mqtt_zoom, goto_preset=mqtt_goto, home=mqtt_home,
            sentry=mqtt_sentry,
        )

    def advertised(name: str, ptz: bool) -> tuple[discovery.Identity, ...]:
        """The ONVIF devices we announce: the media persona first, then the primary.

        The media persona comes first so that a client which takes the first
        match adopts the one that cannot move the camera.  Each persona has its
        own endpoint UUID, its own XAddr and its own scopes, so a client can
        tell them apart — and a probe that asks for a PTZ device is answered by
        the primary persona alone.
        """
        hardware = services.device_identity().model
        identities: list[discovery.Identity] = []
        if read_only_north is not None:
            identities.append(discovery.identity_for(
                options.host, read_only_north.port, onvif.DEVICE_PATH,
                f"{name} ({MEDIA_PERSONA})", ptz=False,
                hardware=hardware, extra_scopes=options.discovery_scopes,
            ))
        identities.append(discovery.identity_for(
            options.host, north.port, onvif.DEVICE_PATH, name, ptz=ptz,
            hardware=hardware, extra_scopes=options.discovery_scopes,
        ))
        return tuple(identities)

    finder = discovery.DiscoveryServer(
        advertised(options.name, False), port=options.discovery_port, multicast=options.announce,
        interface_address=discovery.multicast_address(
            options.discovery_address, options.discovery_interface,
            options.bind_host, options.host,
        ),
        interface_name=options.discovery_interface,
        hello_interval=options.hello_interval if options.announce else 0.0,
    )

    def adopted(camera: Camera) -> None:
        # Register a camera-pinned service as soon as adoption completes. The
        # primary unscoped service remains for backwards-compatible single-camera
        # clients; multi-camera clients use /cameras/{id}/ routes.
        if registry.get(onvif.canonical_camera_id(camera)) is None:
            camera_services = onvif.Services(
                backend_for(camera.mac), host=options.host, port=north.port,
                auth=services.auth, onvif_auth=services.onvif_auth,
                **northbound,  # type: ignore[arg-type]
            )
            registry.register(camera_services)
        if read_only_north is not None and read_only_registry.get(onvif.canonical_camera_id(camera)) is None:
            read_only_services = onvif.Services(
                backend_for(camera.mac), host=options.host, port=read_only_north.port,
                auth=services.auth, onvif_auth=services.onvif_auth, read_only=True,
                persona=MEDIA_PERSONA,
                **northbound,  # type: ignore[arg-type]
            )
            read_only_registry.register(read_only_services)
        log.info(
            "camera ready: %s model=%s ptz=%s pan=%s..%s tilt=%s..%s",
            camera.mac, camera.model or "?", camera.is_ptz,
            camera.pan_range.minimum, camera.pan_range.maximum,
            camera.tilt_range.minimum, camera.tilt_range.maximum,
        )
        if camera.is_ptz and camera.support != "supported":
            log.warning(
                "camera %s model %r is EXPERIMENTAL: PTZ uses its reported ranges only and "
                "FOV-relative moves are off. Run `osprey probe` and open a model-support issue.",
                camera.mac, camera.model or "?",
            )
        # Re-announce now that we know whether to claim PTZ.
        finder.identities = advertised(camera.name or options.name, camera.is_ptz)
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

    last_motion: dict[str, tuple[float, str]] = {}

    def motion_changed(camera: Camera) -> None:
        """Publish PTZ state to MQTT: on every state change, else at most 1/s."""
        if mqtt_runtime is None:
            return
        position, pan_tilt, zoom_state, error = camera.motion.snapshot()
        now = time.monotonic()
        previous = last_motion.get(camera.mac)
        state = f"{pan_tilt}/{zoom_state}/{error}"
        if previous is not None and previous[1] == state and now - previous[0] < 1.0:
            return
        last_motion[camera.mac] = (now, state)
        try:
            mqtt_runtime.publish_state({
                "camera": onvif.canonical_camera_id(camera),
                "state": camera.motion.status,
                "pan_tilt": pan_tilt,
                "zoom": zoom_state,
                "error": error,
                "position": position.as_dict(),
                "at": time.time(),
            })
        except Exception as exc:  # broker trouble must never stall the control loop
            log.debug("MQTT state publish failed: %s", exc)

    control.on_adopted = adopted
    control.on_motion = motion_changed
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
        sentry=sentry_manager,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="osprey", description="Present a UniFi camera as an ONVIF device.")
    parser.add_argument("--version", action="version", version=f"osprey {get_version()}")
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
    if (sys.argv[1:] if argv is None else argv) == ["version"]:
        print(get_version())
        return 0
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
    # Persist a rotating log next to the certificate so a restart keeps a
    # short history.  A read-only or missing state directory only disables the
    # file log; the controller still runs with console logging.
    logfile.install(
        options.cert.parent,
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    def shutdown(signum: int, frame: FrameType | None) -> None:
        # Only flag the stop: the control loop notices it, run() returns, and
        # the teardown below runs on the main thread outside the signal context.
        stack.controller.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info(
        "osprey up: control :%d, ingest :%d, snapshots :%d, rtsp :%d, onvif :%d%s",
        options.control_port, options.ingest_port, options.snapshot_port,
        options.rtsp_port, options.onvif_port, onvif.DEVICE_PATH,
    )
    try:
        stack.run()
    finally:
        log.info("stopping")
        stack.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
