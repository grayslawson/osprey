"""Secure defaults for new installs; unchanged behaviour for existing ones.

``resolve`` picks ``secure`` or ``legacy`` once and remembers the choice in a
marker file in the state directory, so an upgrade never changes what a running
install already does. Secure means: ONVIF and RTSP require generated credentials
unless configured, and the media and snapshot ports accept only the adopted
camera. Legacy means anonymous access and open ports, as before 0.9.

Override with ``OSPREY_SECURITY=secure|legacy``. Anonymous access can also be
chosen per service with ``OSPREY_ONVIF_AUTH=disabled`` / ``OSPREY_RTSP_AUTH=disabled``.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Final

MARKER: Final = "security-mode"
USERNAME: Final = "osprey"

log = logging.getLogger("osprey.security")


def _write_new(path: Path, text: str) -> bool:
    """Create ``path`` 0600 and exclusively; False when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def resolve(state_dir: Path, existing: tuple[Path, ...]) -> str:
    """``secure`` for a fresh state directory, ``legacy`` if any of ``existing`` is there."""
    forced = os.environ.get("OSPREY_SECURITY", "").strip().lower()
    if forced in ("secure", "legacy"):
        return forced
    marker = state_dir / MARKER
    try:
        saved = marker.read_text(encoding="utf-8").strip()
        if saved in ("secure", "legacy"):
            return saved
    except OSError:
        pass
    mode = "legacy" if any(path.exists() for path in existing) else "secure"
    try:
        _write_new(marker, mode + "\n")
    except OSError as exc:
        log.warning("cannot record the security mode in %s: %s", state_dir, exc)
    if mode == "legacy":
        log.warning(
            "existing install detected: keeping the previous open defaults (anonymous ONVIF/RTSP unless "
            "configured, media ports open). Recommended: set OSPREY_SECURITY=secure, or configure "
            "OSPREY_ONVIF_USERNAME/PASSWORD_FILE and OSPREY_RTSP_USERNAME/PASSWORD_FILE and set "
            "cameras[].ip. See docs/security.md."
        )
    return mode


def credential(state_dir: Path, service: str) -> str:
    """The generated password for ``service`` (``onvif`` or ``rtsp``); created on first use, shown once."""
    path = state_dir / f"{service}-password"
    password = secrets.token_urlsafe(24)
    if _write_new(path, password + "\n"):
        log.warning(
            "generated %s credentials (shown once): username=%s password=%s; stored in %s",
            service.upper(), USERNAME, password, path,
        )
        return password
    return path.read_text(encoding="utf-8").strip()
