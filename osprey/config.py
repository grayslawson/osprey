"""osprey runtime configuration — one JSON file, merged under the CLI.

A single JSON file (``osprey.json`` by default, or ``--config PATH``) supplies the
baseline; command-line flags are per-run overrides and **always win**. A missing
file is not an error — Osprey then runs on defaults + CLI exactly as before this
module existed.

Pure and testable: parsing is separate from the filesystem (`load_dict` takes
bytes), and nothing here touches the network or global state.

What became of the original Cuckoo's config fields
----------------------------------------------
The old daemon read a richer ``cuckoo.json`` because it drove the camera over SSH
and toggled many device features. Osprey is narrower — it is *only* the
controller-and-ONVIF seam — so the surface is smaller and some fields are gone by
design:

* ``camera.ssh_user`` / ``ssh_password`` / ``ip`` — **removed.** Osprey never
  SSHes into the camera; the camera dials *us*. Custody hand-off credentials live
  outside the repo (see ``handoff.sh`` and ``OSPREY_SECRETS``), not here.
* ``rtsp.profiles`` (``main``/``medium``/``low``) — became **``tracks``**, the three
  encoder channels ``video1``/``video2``/``video3``, each now naming its **codec**.
* ``bind`` controls the local HTTP/ONVIF bind address; ``host`` remains the
  advertised address camera and clients use. ``ports`` — kept, same idea
  (``control``/``ingest``/``snapshot``/``rtsp``/
  ``onvif``/``discovery``).
* ``controller.uuid`` — no longer needed: Osprey adopts with a null
  ``controllerUuid`` and ``overrideUuid: true`` rather than persisting one.
* ``ptz`` / ``events`` / ``audio`` / ``imaging`` toggles — not yet surfaced here;
  PTZ and events are derived from what the camera announces. They are candidates to
  add back as fields when there is a reason to turn them off.

The one genuinely new idea is **per-channel codec**, because that is what a real
ONVIF client cares about (Home Assistant needs an H.264 profile). The default is
H.264 on every channel; set any to ``h265`` for an efficient stream.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Final
import re

import ptz

DEFAULT_CONFIG_PATH: Final = "osprey.json"
# Pre-0.9 names, still read when the new default file does not exist.
LEGACY_CONFIG_PATH: Final = "cuckoo.json"
LEGACY_CERT_PATH: Final = "cuckoo.pem"


def with_legacy(path: str, legacy: str) -> str:
    """Return *path*, or the pre-rename *legacy* file beside it when only that exists."""
    if os.path.exists(path):
        return path
    old = os.path.join(os.path.dirname(path), legacy)
    return old if os.path.exists(old) else path


def validate_cameras(value: Any) -> list[dict[str, Any]]:
    """Validate the optional explicit camera registry.

    A registry is deliberately keyed by MAC: IP addresses can change, while a
    camera's Protect identity should not.  Keeping validation here gives users a
    useful startup error before any listeners are opened.
    """
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ValueError('"cameras" must be an array of objects')
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f'cameras[{index}] must be an object')
        mac = str(item.get("mac", "")).replace(":", "").replace("-", "").upper()
        if re.fullmatch(r"[0-9A-F]{12}", mac) is None:
            raise ValueError(f'cameras[{index}].mac must be a 12-digit hexadecimal MAC')
        if mac in seen:
            raise ValueError(f'cameras[{index}].mac duplicates another camera')
        seen.add(mac)
        name = item.get("name", mac)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f'cameras[{index}].name must be a non-empty string')
        ip = item.get("ip")
        if ip is not None and (not isinstance(ip, str) or not ip.strip()):
            raise ValueError(f'cameras[{index}].ip must be a non-empty string')
        tracks = item.get("tracks")
        if tracks is not None and not isinstance(tracks, dict):
            raise ValueError(f'cameras[{index}].tracks must be an object')
        result.append({**item, "mac": mac, "name": name.strip()})
    return result


PTZ_LIMITS: Final[dict[str, tuple[float, float]]] = {
    "reply_timeout_sec": (0.5, 300.0),
    "min_silence_sec": (0.0, 300.0),
    "max_unanswered": (1, 20),
    "probe_interval_sec": (0.1, 60.0),
    "idle_probe_sec": (0.0, 3600.0),
    "manual_hold_sec": (0.0, 3600.0),
}


def validate_ptz(value: Any) -> dict[str, Any]:
    """Validate the ``ptz`` block and return it with defaults applied."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('"ptz" must be an object')
    unknown = set(value) - set(DEFAULTS["ptz"])
    if unknown:
        raise ValueError(f'unknown ptz setting(s): {", ".join(sorted(unknown))}')
    out = {**DEFAULTS["ptz"], **value}
    if not isinstance(out["auto_home"], bool):
        raise ValueError('"ptz.auto_home" must be a boolean')
    for key, (low, high) in PTZ_LIMITS.items():
        item = out[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not low <= item <= high:
            raise ValueError(f'"ptz.{key}" must be a number from {low:g} to {high:g}')
    if not isinstance(out["max_unanswered"], int):
        raise ValueError('"ptz.max_unanswered" must be an integer')
    ptz.validate_max_speed(out["max_speed"])
    if 0 < out["idle_probe_sec"] < 5:
        raise ValueError('"ptz.idle_probe_sec" must be 0 (disabled) or at least 5')
    users = out["autotrack_users"]
    if not isinstance(users, list) or not all(isinstance(u, str) and u.strip() for u in users):
        raise ValueError('"ptz.autotrack_users" must be a list of user names')
    baseline = out["isp_baseline"]
    if not isinstance(baseline, dict) or len(baseline) > 32 or not all(
        re.fullmatch(r"[A-Za-z0-9]{1,40}", str(k)) and isinstance(v, (str, int, float, bool))
        for k, v in baseline.items()
    ):
        raise ValueError('"ptz.isp_baseline" must be an object of simple ISP settings')
    presets_file = out["presets_file"]
    if presets_file is not None and (not isinstance(presets_file, str) or not presets_file.strip()):
        raise ValueError('"ptz.presets_file" must be a path or null')
    return out


def validate_runtime(value: dict[str, Any]) -> None:
    """Validate scalar runtime settings before any socket is opened.

    JSON is user input. Failing with one actionable message is preferable to a
    late ``TypeError`` from a server constructor or an accidentally malformed
    URL advertised to Frigate.
    """
    host = value.get("host")
    if host is not None and (
        not isinstance(host, str)
        or not host.strip()
        or len(host) > 255
        or re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", host.strip()) is None
    ):
        raise ValueError('"host" must be a hostname or IP address')
    bind = value.get("bind", "0.0.0.0")
    if not isinstance(bind, str) or not bind.strip() or len(bind.strip()) > 255:
        raise ValueError('"bind" must be an IP address')
    try:
        import ipaddress
        ipaddress.ip_address(bind.strip("[]"))
    except (ValueError, TypeError):
        raise ValueError('"bind" must be an IP address') from None
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise ValueError('"name" must be a non-empty string')
    if not isinstance(value.get("announce"), bool):
        raise ValueError('"announce" must be a boolean')
    tracks = value.get("tracks")
    if not isinstance(tracks, dict) or not tracks:
        raise ValueError('"tracks" must be a non-empty object')
    for name, codec in tracks.items():
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) is None:
            raise ValueError("track names must contain only letters, digits, '.', '_' or '-'")
        if not isinstance(codec, str) or codec.lower() not in {"h264", "h265", "mjpg"}:
            raise ValueError(f'tracks[{name!r}] must be h264, h265, or mjpg')
    validate_ptz(value.get("ptz"))
    ports = value.get("ports")
    if not isinstance(ports, dict):
        raise ValueError('"ports" must be an object')
    required = ("control", "ingest", "snapshot", "rtsp", "onvif", "discovery")
    seen: dict[int, str] = {}
    for key in required + ("onvif_read_only",):
        port = ports.get(key)
        if key == "onvif_read_only" and port is None:
            continue
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError(f'ports.{key} must be an integer from 0 to 65535')
        if port and port in seen:
            raise ValueError(f'ports.{key} duplicates ports.{seen[port]}')
        if port:
            seen[port] = key
    validate_discovery(value.get("discovery", {}))
    validate_onvif(value.get("onvif", {}))
    validate_sentry(value.get("sentry"))
    validate_assist(value.get("assist"))


ASSIST_KEYS: Final[frozenset[str]] = frozenset({"enabled", "url", "token_file", "scopes"})


def validate_assist(value: Any) -> dict[str, Any]:
    """``assist``: the optional Osprey Assist sidecar. Off unless ``enabled`` is true.

    ``url`` is where the console proxy reaches it, ``token_file`` holds the bearer
    token it uses (made on first start when missing), ``scopes`` narrows what it may do.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('"assist" must be an object')
    unknown = set(value) - ASSIST_KEYS
    if unknown:
        raise ValueError(f'unknown assist setting(s): {", ".join(sorted(unknown))}')
    out = {**DEFAULTS["assist"], **value}
    if not isinstance(out["enabled"], bool):
        raise ValueError('"assist.enabled" must be a boolean')
    if not isinstance(out["url"], str) or re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", out["url"]) is None:
        raise ValueError('"assist.url" must be an http or https URL')
    token_file = out["token_file"]
    if token_file is not None and (not isinstance(token_file, str) or not token_file.strip()):
        raise ValueError('"assist.token_file" must be a path or null')
    scopes = out["scopes"]
    if (not isinstance(scopes, list) or not scopes
            or any(item not in ("quality", "presets") for item in scopes)):
        raise ValueError('"assist.scopes" must be a list of "quality" and "presets"')
    return out


def validate_discovery(value: Any) -> None:
    """``discovery``: which LAN interface WS-Discovery uses, Hello cadence, scopes."""
    if not isinstance(value, dict):
        raise ValueError('"discovery" must be an object')
    unknown = set(value) - {"interface", "address", "hello_interval", "scopes"}
    if unknown:
        raise ValueError(f'"discovery" has unknown keys: {", ".join(sorted(unknown))}')
    interface = value.get("interface")
    if interface is not None and (
        not isinstance(interface, str) or re.fullmatch(r"[A-Za-z0-9_.:@-]{1,15}", interface) is None
    ):
        raise ValueError("discovery.interface must be a network interface name")
    address = value.get("address")
    if address is not None:
        import ipaddress
        try:
            parsed = ipaddress.IPv4Address(address)
        except (ValueError, TypeError):
            raise ValueError("discovery.address must be an IPv4 address") from None
        if parsed.is_unspecified or parsed.is_multicast:
            raise ValueError("discovery.address must be a unicast interface address")
    interval = value.get("hello_interval", 300)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not (
        interval == 0 or 30 <= interval <= 86400
    ):
        raise ValueError("discovery.hello_interval must be 0 (off) or 30-86400 seconds")
    scopes = value.get("scopes", [])
    if not isinstance(scopes, list) or len(scopes) > 16:
        raise ValueError("discovery.scopes must be a list of at most 16 scope URIs")
    import discovery
    for scope in scopes:
        discovery.validate_scope(scope)


SENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {"enabled", "interval_sec", "speed", "tracking_hold_sec", "cameras"}
)
SENTRY_CAMERA_KEYS: Final[frozenset[str]] = frozenset(
    {"enabled", "interval_sec", "speed", "tracking_hold_sec", "points"}
)


def _sentry_number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f'"{name}" must be a number')
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'"{name}" must be a number') from exc
    if not low <= number <= high:
        raise ValueError(f'"{name}" must be a number from {low:g} to {high:g}')
    return number


def _sentry_speed(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f'"{name}" must be an integer')
    try:
        speed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'"{name}" must be an integer') from exc
    if not ptz.MIN_SPEED <= speed <= ptz.MAX_SPEED_LIMIT:
        raise ValueError(
            f'"{name}" must be an integer from {ptz.MIN_SPEED} to {ptz.MAX_SPEED_LIMIT}'
        )
    return speed


def _sentry_points(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f'"{name}" must be a list')
    points: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f'"{name}[{index}]" must be an object')
        unknown = set(item) - {"name", "dwell_sec", "night_preset"}
        if unknown:
            raise ValueError(
                f'"{name}[{index}]" has unknown keys: {", ".join(sorted(unknown))}'
            )
        point_name = item.get("name")
        if not isinstance(point_name, str) or not point_name.strip():
            raise ValueError(f'"{name}[{index}].name" must be a non-empty string')
        key = point_name.strip().casefold()
        if key in seen:
            raise ValueError(f'"{name}" repeats the point {point_name.strip()!r}')
        seen.add(key)
        dwell_raw = item.get("dwell_sec")
        dwell = (
            None if dwell_raw is None
            else _sentry_number(dwell_raw, f'"{name}[{index}].dwell_sec"', 1.0, 86400.0)
        )
        night = item.get("night_preset")
        if night is not None and (not isinstance(night, str) or not night.strip()):
            raise ValueError(f'"{name}[{index}].night_preset" must be a non-empty string')
        points.append({
            "name": point_name.strip(), "dwell_sec": dwell,
            **({"night_preset": night.strip()} if night else {}),
        })
    return points


def validate_sentry(value: Any) -> dict[str, Any]:
    """Validate the ``sentry`` block: one idle patrol per camera.

    The block holds defaults that every camera inherits and a ``cameras`` map
    keyed by camera MAC, because each camera gets its own points, cadence and
    speed.  A MAC is the key for the same reason it keys the camera registry:
    an address can change, a camera's identity should not.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('"sentry" must be an object')
    unknown = set(value) - SENTRY_KEYS
    if unknown:
        raise ValueError(f'unknown sentry setting(s): {", ".join(sorted(unknown))}')

    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError('"sentry.enabled" must be a boolean')
    interval = _sentry_number(
        value.get("interval_sec", DEFAULTS["sentry"]["interval_sec"]),
        "sentry.interval_sec", 1.0, 86400.0,
    )
    speed = _sentry_speed(value.get("speed", DEFAULTS["sentry"]["speed"]), "sentry.speed")
    hold = _sentry_number(
        value.get("tracking_hold_sec", DEFAULTS["sentry"]["tracking_hold_sec"]),
        "sentry.tracking_hold_sec", 1.0, 3600.0,
    )

    raw_cameras = value.get("cameras", {})
    if not isinstance(raw_cameras, dict):
        raise ValueError('"sentry.cameras" must be an object keyed by camera MAC')
    cameras: dict[str, Any] = {}
    for key, raw in raw_cameras.items():
        mac = str(key).replace(":", "").replace("-", "").upper()
        if re.fullmatch(r"[0-9A-F]{12}", mac) is None:
            raise ValueError(
                f'"sentry.cameras" key {key!r} must be a 12-digit hexadecimal MAC'
            )
        if mac in cameras:
            raise ValueError(
                f'"sentry.cameras" names the camera {mac} more than once'
            )
        if not isinstance(raw, dict):
            raise ValueError(f'"sentry.cameras.{mac}" must be an object')
        unknown = set(raw) - SENTRY_CAMERA_KEYS
        if unknown:
            raise ValueError(
                f'unknown sentry setting(s) for camera {mac}: {", ".join(sorted(unknown))}'
            )
        camera_enabled = raw.get("enabled", enabled)
        if not isinstance(camera_enabled, bool):
            raise ValueError(f'"sentry.cameras.{mac}.enabled" must be a boolean')
        cameras[mac] = {
            "enabled": camera_enabled,
            "interval_sec": _sentry_number(
                raw.get("interval_sec", interval),
                f"sentry.cameras.{mac}.interval_sec", 1.0, 86400.0,
            ),
            "speed": _sentry_speed(
                raw.get("speed", speed), f"sentry.cameras.{mac}.speed"
            ),
            "tracking_hold_sec": _sentry_number(
                raw.get("tracking_hold_sec", hold),
                f"sentry.cameras.{mac}.tracking_hold_sec", 1.0, 3600.0,
            ),
            "points": _sentry_points(
                raw.get("points", []), f"sentry.cameras.{mac}.points"
            ),
        }
    return {
        "enabled": enabled,
        "interval_sec": interval,
        "speed": speed,
        "tracking_hold_sec": hold,
        "cameras": cameras,
    }


def validate_onvif(value: Any, cameras: list[dict[str, Any]] | None = None) -> None:
    """``onvif``: the northbound device identity (never the camera's MAC)."""
    if not isinstance(value, dict):
        raise ValueError('"onvif" must be an object')
    unknown = set(value) - {"identity", "log_calls"}
    if unknown:
        raise ValueError(f'"onvif" has unknown keys: {", ".join(sorted(unknown))}')
    if not isinstance(value.get("log_calls", False), bool):
        raise ValueError('"onvif.log_calls" must be a boolean')
    import identity
    parsed = identity.IdentityConfig.from_mapping(value.get("identity"))
    if parsed.mac is not None:
        for item in cameras or []:
            mac = str(item.get("mac", ""))
            if len(mac) == 12 and identity.normalise_mac(mac) == parsed.mac:
                raise ValueError("onvif.identity.mac must differ from every camera MAC")


# Every value Osprey reads has a default here, so a config built from {} answers
# everything. Ports mirror the module constants (asserted by the tests).
DEFAULTS: Final[dict[str, Any]] = {
    "host": None,  # the address the camera and clients reach us on; required
    "bind": "0.0.0.0",  # local interface for HTTP/ONVIF listeners
    "name": "osprey",  # controller identity shown to the camera and in discovery
    "frigate": {"url": None},  # optional external Frigate base URL metadata
    "cert": "osprey.pem",
    "announce": True,  # multicast WS-Discovery Hello, or only answer probes
    # Optional allow-list. Empty keeps backwards-compatible discovery of any
    # compatible camera. Each entry requires a stable Protect MAC identity.
    "cameras": [],
    # channel -> codec. Default H.264 everywhere so an ONVIF client (Home
    # Assistant) always finds a profile it can decode; set any to "h265".
    "tracks": {"video1": "h264", "video2": "h264", "video3": "h264"},
    # Optional operator integration; remains inert unless explicitly enabled.
    "mqtt": {"enabled": False},
    # PTZ liveness and preset behaviour. See docs/frigate-integration.md.
    "ptz": {
        # Save the first confirmed idle pose as preset "home" when none exists.
        "auto_home": True,
        # ONVIF speeds map linearly onto 1..max_speed on the G5 scale (1..10000).
        # 1000 is what Protect sends; values above it are unverified on hardware.
        "max_speed": ptz.DEFAULT_MAX_SPEED,
        # Watchdog: recycle the ptz1 socket after `max_unanswered` position
        # requests have gone unanswered and the oldest has been silent for
        # `min_silence_sec`, or the oldest has been silent for `reply_timeout_sec`.
        "reply_timeout_sec": 30.0,
        "min_silence_sec": 10.0,
        "max_unanswered": 3,
        "probe_interval_sec": 2.0,
        "idle_probe_sec": 60.0,
        # Where named positions persist; null = next to the TLS certificate.
        "presets_file": None,
        # Manual wins, then resume: after a person moves the camera (console,
        # MQTT, a non-autotrack ONVIF user), autotracker commands are dropped for
        # this many seconds. 0 disables arbitration. See arbitration.py.
        "manual_hold_sec": 30.0,
        # ChangeIspSettings fields re-sent whenever a PTZ camera (re)connects. hdr 1 fixed a
        # persistent soft/defocused image at the preset zoom on a G5 PTZ whose Protect-set
        # hdr was 2 (2026-11-23); {} sends nothing and leaves the camera's own ISP alone.
        "isp_baseline": {"hdr": 1},
        # ONVIF users treated as autotrackers. Anonymous ONVIF is always one.
        "autotrack_users": ["frigate"],
    },
    # Northbound ONVIF identity overrides; unset values are derived and never
    # equal the physical camera's MAC or serial (see identity.py).
    "onvif": {"identity": {}},
    # Idle patrol ("sentry"). The camera sweeps a list of named presets and
    # steps aside for a person or an autotracker. Each camera has its own
    # points, cadence and speed under "cameras", keyed by camera MAC.
    # See docs/sentry.md.
    "sentry": {
        "enabled": False,
        "interval_sec": 45,
        "speed": ptz.DEFAULT_SPEED,
        "tracking_hold_sec": 15,
        "cameras": {},
    },
    # Osprey Assist, the optional image-quality sidecar (docs/assist.md). The core
    # only opens a token-authenticated door for it and proxies the console tab.
    "assist": {
        "enabled": False,
        "url": "http://127.0.0.1:8090",
        "token_file": None,
        "scopes": ["quality", "presets"],
    },
    # WS-Discovery: pin multicast to one LAN interface/address, repeat Hello
    # every hello_interval seconds (0 = only at startup), add scopes.
    "discovery": {"interface": None, "address": None, "hello_interval": 300, "scopes": []},
    "ports": {
        "control": 7442,
        "ingest": 7550,
        "snapshot": 7444,
        "rtsp": 8554,
        "onvif": 8000,
        # Optional second ONVIF device for the same camera: the media persona,
        # which carries media but no PTZ, for an NVR such as UniFi Protect.
        "onvif_read_only": None,
        "discovery": 3702,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay ``override`` onto a copy of ``base``.

    Dicts merge key-by-key; every other value (including lists) replaces wholesale.
    So a file that sets ``ports.onvif`` leaves the other ports alone, and a file
    that sets ``tracks.video2`` re-codecs just that channel.
    """
    # A deep copy: callers mutate the result, and that must never reach DEFAULTS.
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_dict(raw: bytes) -> dict[str, Any]:
    """Parse config bytes into a dict. Raises on non-object JSON."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def load(path: str) -> dict[str, Any]:
    """Read and parse a config file; a missing file is an empty config."""
    try:
        with open(path, "rb") as handle:
            return load_dict(handle.read())
    except FileNotFoundError:
        return {}


def merged(file_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The defaults with a parsed config file overlaid."""
    return deep_merge(DEFAULTS, file_config or {})
