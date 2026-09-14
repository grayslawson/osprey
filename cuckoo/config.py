"""cuckoo runtime configuration — one JSON file, merged under the CLI.

A single JSON file (``cuckoo.json`` by default, or ``--config PATH``) supplies the
baseline; command-line flags are per-run overrides and **always win**. A missing
file is not an error — cuckoo then runs on defaults + CLI exactly as before this
module existed.

Pure and testable: parsing is separate from the filesystem (`load_dict` takes
bytes), and nothing here touches the network or global state.

What became of the old cuckoo's config fields
----------------------------------------------
The old daemon read a richer ``cuckoo.json`` because it drove the camera over SSH
and toggled many device features. This cuckoo is narrower — it is *only* the
controller-and-ONVIF seam — so the surface is smaller and some fields are gone by
design:

* ``camera.ssh_user`` / ``ssh_password`` / ``ip`` — **removed.** This cuckoo never
  SSHes into the camera; the camera dials *us*. Custody hand-off credentials live
  outside the repo (see ``handoff.sh`` and ``CUCKOO_SECRETS``), not here.
* ``rtsp.profiles`` (``main``/``medium``/``low``) — became **``tracks``**, the three
  encoder channels ``video1``/``video2``/``video3``, each now naming its **codec**.
* ``ports`` — kept, same idea (``control``/``ingest``/``snapshot``/``rtsp``/
  ``onvif``/``discovery``).
* ``controller.uuid`` — no longer needed: this cuckoo adopts with a null
  ``controllerUuid`` and ``overrideUuid: true`` rather than persisting one.
* ``ptz`` / ``events`` / ``audio`` / ``imaging`` toggles — not yet surfaced here;
  PTZ and events are derived from what the camera announces. They are candidates to
  add back as fields when there is a reason to turn them off.

The one genuinely new idea is **per-channel codec**, because that is what a real
ONVIF client cares about (Home Assistant needs an H.264 profile). The default is
H.264 on every channel; set any to ``h265`` for an efficient stream.
"""

from __future__ import annotations

import json
from typing import Any, Final
import re

DEFAULT_CONFIG_PATH: Final = "cuckoo.json"


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

# Every value cuckoo reads has a default here, so a config built from {} answers
# everything. Ports mirror the module constants (asserted by the tests).
DEFAULTS: Final[dict[str, Any]] = {
    "host": None,  # the address the camera and clients reach us on; required
    "name": "cuckoo",  # controller identity shown to the camera and in discovery
    "cert": "cuckoo.pem",
    "announce": True,  # multicast WS-Discovery Hello, or only answer probes
    # Optional allow-list. Empty keeps backwards-compatible discovery of any
    # compatible camera. Each entry requires a stable Protect MAC identity.
    "cameras": [],
    # channel -> codec. Default H.264 everywhere so an ONVIF client (Home
    # Assistant) always finds a profile it can decode; set any to "h265".
    "tracks": {"video1": "h264", "video2": "h264", "video3": "h264"},
    # Optional operator integration; remains inert unless explicitly enabled.
    "mqtt": {"enabled": False},
    "ports": {
        "control": 7442,
        "ingest": 7550,
        "snapshot": 7444,
        "rtsp": 8554,
        "onvif": 8000,
        "discovery": 3702,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay ``override`` onto a copy of ``base``.

    Dicts merge key-by-key; every other value (including lists) replaces wholesale.
    So a file that sets ``ports.onvif`` leaves the other ports alone, and a file
    that sets ``tracks.video2`` re-codecs just that channel.
    """
    out = dict(base)
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
