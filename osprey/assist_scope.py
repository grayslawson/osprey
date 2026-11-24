"""What the Osprey Assist token may do.

Assist is a separate process that talks to the HTTP API with its own bearer
token (``OSPREY_ASSIST_TOKEN_FILE``). The token is not an operator session:
it skips the cookie and CSRF checks, tags its moves ``ASSIST`` and may reach
only the routes listed here. The core enforces this; Assist's own validator is
the second line.

Three scopes exist. ``quality`` covers reading, moving, going to presets, ISP and
the patrol hold. ``presets`` adds writing presets and a patrol's night view, the
structural changes. ``patrol`` adds rewriting the patrol's points and deleting a
preset, which Survey needs. ``OSPREY_ASSIST_SCOPES`` (comma separated) picks them;
all are on by default.

Pure and standard library only, so tests need no server.
"""

from __future__ import annotations

import math
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SCOPE_QUALITY: Final = "quality"
SCOPE_PRESETS: Final = "presets"
SCOPE_PATROL: Final = "patrol"
SCOPES: Final = (SCOPE_QUALITY, SCOPE_PRESETS, SCOPE_PATROL)

# The camera accepts these keys and ignores them. Nothing may send them, whatever the model says.
ISP_DENY: Final = frozenset({"focusPosition", "irLedLevel", "irLedMode", "wdr"})
# Sanity bounds, not camera specifications: they stop a runaway value reaching the camera.
# Assist keeps the tighter per-lever limits and step sizes.
ISP_ALLOW: Final[dict[str, tuple[float, float]]] = {
    "hdr": (0, 3),
    "gain": (0, 10000),
    "shutter": (0, 100000),
    "aeTargetPercent": (1, 100),
    "brightness": (0, 100),
    "contrast": (0, 100),
    "sharpness": (0, 100),
    "denoise": (0, 100),
    "icrSensitivity": (0, 100),
    "touchFocusX": (0, 10000),
    "touchFocusY": (0, 10000),
}

# (method, camera-relative path, scope). ``None`` as the path prefix means a top-level route.
_CAMERA: Final = r"/cameras/[^/]+"
_ROUTES: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = tuple(
    (method, re.compile(pattern), scope) for method, pattern, scope in (
        ("GET", r"/cameras", SCOPE_QUALITY),
        ("GET", r"/events", SCOPE_QUALITY),
        ("GET", _CAMERA + r"/(state|snapshot\.jpg|presets|sentry)", SCOPE_QUALITY),
        ("POST", _CAMERA + r"/(ptz/move|ptz/center|isp)", SCOPE_QUALITY),
        ("POST", _CAMERA + r"/presets/\d{1,6}/goto", SCOPE_QUALITY),
        ("POST", _CAMERA + r"/sentry/(hold|dwell)", SCOPE_QUALITY),
        ("DELETE", _CAMERA + r"/sentry/(hold|dwell)", SCOPE_QUALITY),
        ("POST", _CAMERA + r"/presets", SCOPE_PRESETS),
        ("PUT", _CAMERA + r"/presets/\d{1,6}", SCOPE_PRESETS),
        ("POST", _CAMERA + r"/sentry", SCOPE_PRESETS),
        ("DELETE", _CAMERA + r"/presets/\d{1,6}", SCOPE_PATROL),
    )
)
# Routes that move the head or write a preset, so they take the movement lease.
_MOVES: Final = re.compile(_CAMERA + r"/(ptz/move|ptz/center|presets|presets/\d{1,6}|presets/\d{1,6}/goto)")


# What the console may forward to Assist (``/api/v1/assist/...``). Nothing else is proxied.
PROXY_ROUTES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (method, re.compile(pattern)) for method, pattern in (
        ("GET", r"/(status|settings|decisions|decisions/\d{1,12}|frames/[A-Za-z0-9_.-]{1,80}\.jpg)"),
        ("GET", r"/survey/(status|maps|map|proposals|frigate\.json|(panorama\.jpg|overlay\.png|preview\.jpg)"
                r"|proposals/\d{1,12}/preview\.jpg)"),
        ("POST", r"/(settings|check|decisions/\d{1,12}/(approve|reject|snooze|undo))"),
        ("POST", r"/survey/(start|cancel|resume|apply|revert|proposals/\d{1,12}/(accept|reject|goto))"),
        ("DELETE", r"/data"),
    )
)
PROXY_QUERY: Final = frozenset({"status", "limit", "before", "camera", "capability", "kind", "pan", "tilt", "zoom", "map"})
PROXY_MAX_BYTES: Final = 8 * 1024 * 1024
PROXY_TIMEOUT_SEC: Final = 15.0


@dataclass(frozen=True)
class AssistLink:
    """How the core talks to, and trusts, an Assist process."""

    token: str
    scopes: frozenset[str]
    url: str


def proxy_allowed(method: str, rest: str) -> bool:
    return any(allowed == method and pattern.fullmatch(rest) for allowed, pattern in PROXY_ROUTES)


def route_scope(method: str, path: str) -> str | None:
    """The scope a request needs, or ``None`` when the token may not call it at all."""
    for allowed, pattern, scope in _ROUTES:
        if allowed == method and pattern.fullmatch(path):
            return scope
    return None


def takes_lease(method: str, path: str) -> bool:
    return method in ("POST", "PUT") and _MOVES.fullmatch(path) is not None


def parse_scopes(text: str | None) -> frozenset[str]:
    """``quality,presets`` to a set; an empty or missing value means both."""
    if text is None or not text.strip():
        return frozenset(SCOPES)
    chosen = {part.strip().lower() for part in text.split(",") if part.strip()}
    unknown = chosen - set(SCOPES)
    if unknown:
        raise ValueError(f"OSPREY_ASSIST_SCOPES: unknown scope(s) {', '.join(sorted(unknown))}; use {', '.join(SCOPES)}")
    return frozenset(chosen)


def check_isp(data: object) -> dict[str, object]:
    """Validate an ISP write from the Assist token: allowlisted keys, finite numbers in range."""
    if not isinstance(data, dict) or not data or len(data) > len(ISP_ALLOW):
        raise ValueError("expected a JSON object of ISP settings")
    for key, value in data.items():
        if key in ISP_DENY:
            raise ValueError(f"{key} is not adjustable on this camera")
        bounds = ISP_ALLOW.get(key)
        if bounds is None:
            raise ValueError(f"{key} is not an ISP setting Assist may change")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a number")
        if not bounds[0] <= value <= bounds[1]:
            raise ValueError(f"{key} must be between {bounds[0]:g} and {bounds[1]:g}")
    return data


def load_token(path: str | os.PathLike[str], *, create: bool = True) -> str:
    """Read the token file, making it (0600, exclusively) on first use."""
    target = Path(path)
    if create and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(32) + "\n")
    token = target.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise ValueError(f"{target} must hold a token of at least 16 characters")
    return token
