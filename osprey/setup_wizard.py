"""First-run setup state and validation.

The wizard deliberately keeps credentials out of ``osprey.json``.  It writes a
small, mode-0600 environment file and separate password files that can be
mounted into a container or loaded by a service manager on the next restart.
The setup token is generated per process, logged at startup as a ready-to-open
link, and must accompany every unauthenticated setup request. It is single use
(consumed by the successful save), expires after ``TOKEN_TTL_SEC`` (a fresh one
is then logged), and only exists while no host is configured.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import logging
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import config
from frigate_config import validate_url


TOKEN_TTL_SEC: Final = 3600
# Present while an automatically opened wizard is unfinished, so a restart
# mid-setup reopens it even though the first start already made a certificate.
PENDING_MARKER: Final = "setup-pending"

log = logging.getLogger("osprey.setup")


class SetupError(ValueError):
    """A user-correctable setup form error."""


def detect_lan_ip() -> str | None:
    """The IPv4 source address of the default route, or None without one.

    Connecting a UDP socket only selects a route; no packet is sent. The
    destination is a documentation address, so it is never a real peer.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = str(probe.getsockname()[0])
    except OSError:
        return None
    return None if address.startswith(("0.", "127.")) else address


def bridge_hint(address: str | None) -> bool:
    """True when ``address`` looks like a container bridge, not the host's LAN.

    shortcut: Docker and Podman default ranges only; a LAN on 172.16/12 inside a
    container also matches, which only shows a hint in the wizard.
    """
    if not address or not (Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()):
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip in ipaddress.ip_network("172.16.0.0/12") or ip in ipaddress.ip_network("10.88.0.0/16")


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write *data* durably without exposing a partially-written secret."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_write_set(artifacts: dict[Path, bytes], mode: int = 0o600) -> None:
    """Commit related files together and roll back if a replacement fails.

    A setup submission creates several files that must describe the same deployment.  The
    filesystem cannot atomically replace multiple paths, so retain old contents in memory and
    restore every path already replaced when a later replacement fails.
    """

    staged: dict[Path, Path] = {}
    previous: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    try:
        for path, data in artifacts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            previous[path] = path.read_bytes() if path.exists() else None
            fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            staged[path] = Path(temporary_name)
        for path, temporary_path in staged.items():
            os.replace(temporary_path, path)
            os.chmod(path, mode)
            replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            old = previous[path]
            if old is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                _atomic_write(path, old, mode)
        raise
    finally:
        for temporary_path in staged.values():
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _password_hash(password: str) -> str:
    """Return the same PBKDF2 format used by :class:`onvif.AdminAuth`."""

    iterations = 310_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def _host(value: object, field: str, *, allow_wildcard: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SetupError(f"{field} is required")
    value = value.strip()
    if len(value) > 255 or any(ch in value for ch in " /\\\t\r\n"):
        raise SetupError(f"{field} must be an IP address or DNS hostname")
    if allow_wildcard and value in ("0.0.0.0", "::"):
        return value
    try:
        ipaddress.ip_address(value.strip("[]"))
        return value
    except ValueError:
        # DNS names are accepted for advertised host, while bind addresses
        # should remain explicit to avoid surprising wildcard exposure.
        if field == "bind address":
            raise SetupError("bind address must be an IP address") from None
        import re

        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,253}[A-Za-z0-9])?", value):
            raise SetupError(f"{field} must be an IP address or DNS hostname") from None
        return value


def _camera_mac(value: object) -> str:
    if not isinstance(value, str):
        raise SetupError("camera MAC address is required")
    normalized = value.replace(":", "").replace("-", "").strip().upper()
    import re

    if not re.fullmatch(r"[0-9A-F]{12}", normalized):
        raise SetupError("camera MAC address must contain 12 hexadecimal digits")
    return normalized


def _camera_ip(value: object) -> str:
    """The camera's address, or "" when left blank (optional: discovery is not restricted)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if not isinstance(value, str):
        raise SetupError("camera IP address must be text")
    value = value.strip()
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise SetupError("camera IP address must be a valid IPv4 or IPv6 address") from exc
    return value


def _name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise SetupError("camera name must be between 1 and 64 characters")
    return value.strip()


def _frigate_url(value: object) -> str:
    """A validated Frigate URL, or "" when left blank (Frigate can be added later)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if not isinstance(value, str):
        raise SetupError("Frigate URL must be text")
    try:
        return validate_url(value)
    except ValueError as exc:
        raise SetupError(str(exc)) from exc


class SetupManager:
    """Own the one-time setup token and persistent setup artifacts."""

    def __init__(
        self,
        config_path: str | Path,
        secrets_path: str | Path | None = None,
        frigate_path: str | Path | None = None,
        token: str | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        state_dir = self.config_path.parent
        self.secrets_path = Path(secrets_path) if secrets_path else state_dir / "osprey-secrets.env"
        self.frigate_path = Path(frigate_path) if frigate_path else state_dir / "frigate.json"
        self.token = token or secrets.token_urlsafe(24)
        self.issued = time.monotonic()
        # Set by the daemon: where the wizard is served, what to prefill, and
        # what to do once a configuration has been written (restart).
        self.url = ""
        self.defaults: dict[str, object] = {}
        self.on_complete: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self.completed = self._config_complete()

    def _config_complete(self) -> bool:
        try:
            value = config.merged(config.load(str(self.config_path)))
            return bool(value.get("host"))
        except (OSError, ValueError, TypeError):
            return False

    @property
    def pending(self) -> bool:
        return not self.completed

    def link(self) -> str:
        return f"{self.url}?token={self.token}" if self.url else ""

    def announce(self) -> None:
        """Log the ready-to-open wizard link in a banner that stands out in a log."""
        rule = "=" * 72
        log.warning(
            "\n%s\n  Osprey first-run setup. Open this link in a browser:\n\n    %s\n\n"
            "  The link is single use and expires in %d minutes; a new one is logged then.\n%s",
            rule, self.link() or "/setup?token=" + self.token, TOKEN_TTL_SEC // 60, rule,
        )

    def check_token(self, supplied: str, *, trusted: bool = False) -> None:
        """Accept the current token, or a caller already trusted (Home Assistant ingress)."""
        if self.completed:
            raise SetupError("setup is already complete; edit the configuration and restart")
        if trusted:
            return
        with self._lock:
            if time.monotonic() - self.issued > TOKEN_TTL_SEC:
                self.token, self.issued = secrets.token_urlsafe(24), time.monotonic()
                expired = True
            else:
                expired = False
        if expired:
            self.announce()
            raise SetupError("the setup link has expired; open the new link from the Osprey log")
        if not hmac.compare_digest(supplied.encode(), self.token.encode()):
            raise SetupError("invalid setup token; open the setup link from the Osprey log")

    def status(self) -> dict[str, object]:
        return {
            "pending": self.pending,
            "config_path": str(self.config_path),
            "secrets_path": str(self.secrets_path),
            "restart_required": False,
            **self.defaults,
        }

    def save(self, value: object, supplied_token: str, *, trusted: bool = False) -> dict[str, object]:
        self.check_token(supplied_token, trusted=trusted)
        if not isinstance(value, dict):
            raise SetupError("setup payload must be an object")
        advertised = _host(value.get("host"), "advertised host")
        bind = _host(value.get("bind", "0.0.0.0"), "bind address", allow_wildcard=True)
        name = _name(value.get("name") or value.get("camera_name"))
        mac = _camera_mac(value.get("mac"))
        camera_ip = _camera_ip(value.get("ip"))
        frigate = _frigate_url(value.get("frigate_url") or value.get("frigate"))
        password = value.get("admin_password")
        if not isinstance(password, str) or len(password) < 8:
            raise SetupError("admin password must be at least 8 characters")

        # Generate credentials for RTSP and ONVIF; both require them from the start.
        rtsp_user = "osprey"
        rtsp_password = secrets.token_urlsafe(24)
        # The name Frigate's autotracking is recognised by (ptz.autotrack_users).
        onvif_user = "frigate"
        onvif_password = secrets.token_urlsafe(24)
        admin_hash = _password_hash(password)
        generated = {
            "rtsp_username": rtsp_user,
            "rtsp_password": rtsp_password,
            "onvif_username": onvif_user,
            "onvif_password": onvif_password,
        }
        cert_path = self.config_path.parent / "osprey.pem"
        runtime = {
            "host": advertised,
            "bind": bind,
            "name": name,
            "cert": str(cert_path),
            "announce": True,
            **({"frigate": {"url": frigate}} if frigate else {}),
            "cameras": [{"mac": mac, "name": name, **({"ip": camera_ip} if camera_ip else {})}],
            "tracks": {"video1": "h264", "video2": "h264", "video3": "h264"},
            "ports": dict(config.DEFAULTS["ports"]),
        }
        config.validate_runtime(config.merged(runtime))
        frigate_data = json.dumps({"base_url": frigate}, indent=2).encode() + b"\n"
        secrets_lines = [
            "# Generated by Osprey first-run wizard; keep mode 0600.",
            f"OSPREY_ADMIN_PASSWORD_HASH={admin_hash}",
            f"OSPREY_RTSP_USERNAME={rtsp_user}",
            f"OSPREY_RTSP_PASSWORD_FILE={self.secrets_path.parent / 'rtsp-password'}",
            # Always set, so a Frigate URL entered later in the wizard or console persists.
            f"OSPREY_FRIGATE_CONFIG_FILE={self.frigate_path}",
            f"OSPREY_ONVIF_USERNAME={onvif_user}",
            f"OSPREY_ONVIF_PASSWORD_FILE={self.secrets_path.parent / 'onvif-password'}",
        ]
        artifacts = {
            self.config_path: json.dumps(runtime, indent=2).encode() + b"\n",
            self.secrets_path: ("\n".join(secrets_lines) + "\n").encode(),
            self.secrets_path.parent / "rtsp-password": (rtsp_password + "\n").encode(),
            self.secrets_path.parent / "onvif-password": (onvif_password + "\n").encode(),
        }
        if frigate:
            artifacts[self.frigate_path] = frigate_data
        with self._lock:
            _atomic_write_set(artifacts)
            self.completed = True
        try:
            (self.config_path.parent / PENDING_MARKER).unlink()
        except FileNotFoundError:
            pass
        if self.on_complete is not None:
            self.on_complete()
        # Passwords are returned exactly once to the browser response; callers
        # should not log this object.
        return {
            "ok": True,
            "config_path": str(self.config_path),
            "secrets_path": str(self.secrets_path),
            "restart_required": self.on_complete is None,
            "restarting": self.on_complete is not None,
            "generated_credentials": generated,
        }


def load_environment(path: str | Path) -> None:
    """Load generated ``KEY=value`` deployment secrets without shell eval."""

    try:
        secret_path = Path(path)
        if secret_path.stat().st_mode & 0o077:
            return
        lines = secret_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key.replace("_", "").isalnum() and key.isupper():
            # Explicit process environment always wins over generated values.
            os.environ.setdefault(key, value.strip().strip("\"'"))
