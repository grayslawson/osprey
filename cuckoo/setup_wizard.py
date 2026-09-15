"""First-run setup state and validation.

The wizard deliberately keeps credentials out of ``cuckoo.json``.  It writes a
small, mode-0600 environment file and separate password files that can be
mounted into a container or loaded by a service manager on the next restart.
The setup token is generated per process and must be copied from the startup
log before the unauthenticated setup POST is accepted.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any
import config
from frigate_config import validate_url


class SetupError(ValueError):
    """A user-correctable setup form error."""


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
            raise SetupError("bind address must be an IP address")
        import re

        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,253}[A-Za-z0-9])?", value):
            raise SetupError(f"{field} must be an IP address or DNS hostname")
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
    if not isinstance(value, str) or not value.strip():
        raise SetupError("camera IP address is required")
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
    if not isinstance(value, str) or not value.strip():
        raise SetupError("Frigate URL is required")
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

    def status(self) -> dict[str, object]:
        return {
            "pending": self.pending,
            "config_path": str(self.config_path),
            "secrets_path": str(self.secrets_path),
            "restart_required": self.completed,
        }

    def save(self, value: object, supplied_token: str) -> dict[str, object]:
        if self.completed:
            raise SetupError("setup is already complete; edit the configuration and restart")
        if not hmac.compare_digest(supplied_token, self.token):
            raise SetupError("invalid setup token; copy the token printed at startup")
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

        # Generate credentials for media consumers, but leave ONVIF disabled by
        # default because Frigate deployments commonly start in anonymous mode.
        rtsp_user = "osprey"
        rtsp_password = secrets.token_urlsafe(24)
        onvif_user = "osprey"
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
            "frigate": {"url": frigate},
            "cameras": [{"mac": mac, "name": name, "ip": camera_ip}],
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
            f"OSPREY_FRIGATE_CONFIG_FILE={self.frigate_path}",
            "# ONVIF stays anonymous for Frigate compatibility. Enable explicitly after testing:",
            f"# OSPREY_ONVIF_USERNAME={onvif_user}",
            f"# OSPREY_ONVIF_PASSWORD_FILE={self.secrets_path.parent / 'onvif-password'}",
        ]
        with self._lock:
            _atomic_write(self.config_path, json.dumps(runtime, indent=2).encode() + b"\n")
            _atomic_write(self.frigate_path, frigate_data)
            _atomic_write(self.secrets_path, ("\n".join(secrets_lines) + "\n").encode())
            _atomic_write(self.secrets_path.parent / "rtsp-password", (rtsp_password + "\n").encode())
            _atomic_write(self.secrets_path.parent / "onvif-password", (onvif_password + "\n").encode())
            self.completed = True
        # Passwords are returned exactly once to the browser response; callers
        # should not log this object.
        return {
            "ok": True,
            "config_path": str(self.config_path),
            "secrets_path": str(self.secrets_path),
            "restart_required": True,
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
