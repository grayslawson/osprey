"""Northbound identities: who may use ONVIF, RTSP and snapshots, and for what.

Osprey serves several independent clients at once — typically Frigate (media,
events and PTZ) and UniFi Protect as a third-party ONVIF camera (media and
events only). Each client gets its own principal so that a credential can be
revoked or narrowed without touching the others.

A *users file* (``OSPREY_ONVIF_USERS_FILE``) is a JSON object::

    {
      "users": [
        {"username": "frigate", "password_file": "/run/secrets/osprey-frigate",
         "roles": ["media", "events", "ptz"]},
        {"username": "protect", "password_file": "/run/secrets/osprey-protect",
         "roles": ["media", "events"], "allow_from": ["192.0.2.30/32"]},
        {"username": "admin", "password_file": "/run/secrets/osprey-admin",
         "roles": ["admin"]}
      ],
      "rtsp": {"require_auth": true, "anonymous_from": []}
    }

Roles:

``media``
    Device/media metadata, stream and snapshot URIs, RTSP playback, snapshots.
``events``
    Pull-point event subscriptions.
``ptz``
    PTZ status, movement, Stop and preset changes.
``admin``
    Everything, including configuration writes such as
    ``SetVideoEncoderConfiguration``.

Any authenticated principal may read basic device information (GetDeviceInformation,
GetScopes, GetNetworkInterfaces …): every ONVIF client needs those to identify
the device.

``allow_from`` (optional) restricts a principal to source addresses or CIDRs. It
applies to ONVIF, RTSP and snapshot requests alike. ``rtsp.anonymous_from`` lets
specific hosts play RTSP without Digest credentials while RTSP auth is otherwise
required — a fallback for an NVR that does not send RTSP credentials (to be
confirmed by packet capture; see ``docs/protect-onvif.md``).

``replay_protection`` (default ``true``) rejects a WS-Security nonce that has
already been accepted. Setting it to ``false`` for one principal weakens that
principal's protection against captured-request replay inside the clock-skew
window and exists only as an escape hatch for a client that is proven to reuse
nonces.

Pure: parsing takes data and an injectable file reader, so tests need no files.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Final, Iterable
from urllib.parse import urlsplit

ROLE_MEDIA: Final = "media"
ROLE_EVENTS: Final = "events"
ROLE_PTZ: Final = "ptz"
ROLE_ADMIN: Final = "admin"
ROLES: Final = frozenset({ROLE_MEDIA, ROLE_EVENTS, ROLE_PTZ, ROLE_ADMIN})
# Sentinel requirement: any authenticated principal.
ANY_USER: Final = "user"

USERNAME_RE: Final = re.compile(r"[A-Za-z0-9._@-]{1,64}")
MAX_PASSWORD_LEN: Final = 256
MAX_USERS: Final = 32

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(value: object, where: str) -> tuple[Network, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError(f"{where} must be a list of at most 64 addresses or CIDRs")
    out: list[Network] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{where} entries must be strings")
        try:
            out.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError as exc:
            raise ValueError(f"{where}: {item!r} is not an address or CIDR") from exc
    return tuple(out)


def peer_in(peer: str, networks: Iterable[Network]) -> bool:
    try:
        address = ipaddress.ip_address(peer.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


@dataclass(frozen=True)
class Principal:
    """One northbound client identity."""

    username: str
    password: str = field(repr=False)
    roles: frozenset[str] = frozenset({ROLE_ADMIN})
    allow_from: tuple[Network, ...] = ()
    replay_protection: bool = True

    def has(self, role: str) -> bool:
        if role == ANY_USER:
            return True
        return ROLE_ADMIN in self.roles or role in self.roles

    def permits_peer(self, peer: str) -> bool:
        return not self.allow_from or peer_in(peer, self.allow_from)


def _read_secret(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def parse_principal(
    item: object, index: int, reader: Callable[[str], str] = _read_secret
) -> Principal:
    where = f"users[{index}]"
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    unknown = set(item) - {"username", "password", "password_file", "roles", "allow_from", "replay_protection"}
    if unknown:
        raise ValueError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    username = item.get("username")
    if not isinstance(username, str) or USERNAME_RE.fullmatch(username) is None:
        raise ValueError(f"{where}.username must be 1-64 letters, digits, '.', '_', '@' or '-'")
    if ("password" in item) == ("password_file" in item):
        raise ValueError(f"{where} needs exactly one of password or password_file")
    if "password_file" in item:
        path = item["password_file"]
        if not isinstance(path, str) or not path:
            raise ValueError(f"{where}.password_file must be a path")
        try:
            password = reader(path)
        except OSError as exc:
            raise ValueError(f"{where}.password_file cannot be read: {exc.strerror or exc}") from exc
    else:
        password = item["password"]
    if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LEN:
        raise ValueError(f"{where} password must be 1-{MAX_PASSWORD_LEN} characters")
    if any(ord(character) < 0x20 for character in password):
        raise ValueError(f"{where} password must not contain control characters")
    roles_value = item.get("roles", [ROLE_ADMIN])
    if not isinstance(roles_value, list) or not roles_value or not all(isinstance(r, str) for r in roles_value):
        raise ValueError(f"{where}.roles must be a non-empty list of role names")
    roles = frozenset(roles_value)
    if roles - ROLES:
        raise ValueError(f"{where}.roles has unknown roles: {', '.join(sorted(roles - ROLES))}")
    replay = item.get("replay_protection", True)
    if not isinstance(replay, bool):
        raise ValueError(f"{where}.replay_protection must be a boolean")
    return Principal(
        username=username,
        password=password,
        roles=roles,
        allow_from=parse_networks(item.get("allow_from"), f"{where}.allow_from"),
        replay_protection=replay,
    )


@dataclass(frozen=True)
class UserDirectory:
    """Every configured principal, plus RTSP policy."""

    users: tuple[Principal, ...] = ()
    rtsp_require_auth: bool = False
    rtsp_anonymous_from: tuple[Network, ...] = ()

    def get(self, username: str) -> Principal | None:
        return next((user for user in self.users if user.username == username), None)

    @property
    def enabled(self) -> bool:
        return bool(self.users)

    def with_user(self, principal: Principal) -> "UserDirectory":
        if self.get(principal.username) is not None:
            raise ValueError(f"user {principal.username!r} is configured twice")
        return UserDirectory(self.users + (principal,), self.rtsp_require_auth, self.rtsp_anonymous_from)

    @classmethod
    def parse(cls, data: object, reader: Callable[[str], str] = _read_secret) -> "UserDirectory":
        if not isinstance(data, dict):
            raise ValueError("users file must be a JSON object")
        unknown = set(data) - {"users", "rtsp"}
        if unknown:
            raise ValueError(f"users file has unknown keys: {', '.join(sorted(unknown))}")
        items = data.get("users", [])
        if not isinstance(items, list) or not items or len(items) > MAX_USERS:
            raise ValueError(f'"users" must be a list of 1-{MAX_USERS} users')
        users: list[Principal] = []
        for index, item in enumerate(items):
            principal = parse_principal(item, index, reader)
            if any(user.username == principal.username for user in users):
                raise ValueError(f"users[{index}].username duplicates another user")
            users.append(principal)
        rtsp = data.get("rtsp", {})
        if not isinstance(rtsp, dict) or set(rtsp) - {"require_auth", "anonymous_from"}:
            raise ValueError('"rtsp" must be an object with require_auth and anonymous_from')
        require = rtsp.get("require_auth", False)
        if not isinstance(require, bool):
            raise ValueError("rtsp.require_auth must be a boolean")
        return cls(
            users=tuple(users),
            rtsp_require_auth=require,
            rtsp_anonymous_from=parse_networks(rtsp.get("anonymous_from"), "rtsp.anonymous_from"),
        )

    @classmethod
    def load(cls, path: str) -> "UserDirectory":
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read users file: {exc.strerror or exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"users file is not valid JSON (line {exc.lineno})") from exc
        return cls.parse(data)


# ---------------------------------------------------------------- HTTP Digest


def _digest_fields(header: str) -> dict[str, str]:
    """Parse ``Digest k=v, k="v,with,commas"`` without splitting inside quotes."""
    values: dict[str, str] = {}
    for match in re.finditer(r'([A-Za-z0-9_-]+)\s*=\s*("((?:[^"\\]|\\.)*)"|[^,\s]*)', header):
        key = match.group(1).lower()
        value = match.group(3) if match.group(3) is not None else match.group(2)
        values.setdefault(key, value)
    return values


def _uri_target(uri: str) -> tuple[str, str]:
    """Reduce an absolute or relative request URI to (path, query)."""
    parts = urlsplit(uri)
    path = parts.path or "/"
    return path.rstrip("/") or "/", parts.query


def same_target(digest_uri: str, request_uri: str) -> bool:
    """Clients disagree on absolute vs relative ``uri=`` values; compare targets."""
    return digest_uri == request_uri or _uri_target(digest_uri) == _uri_target(request_uri)


class DigestVerifier:
    """RFC 7616 MD5 Digest (qop=auth or legacy RFC 2069) with expiring nonces."""

    MAX_NONCES: Final[int] = 1024

    def __init__(self, realm: str, lookup: Callable[[str], Principal | None], nonce_ttl: int = 300) -> None:
        self.realm = realm
        self.lookup = lookup
        self.nonce_ttl = nonce_ttl
        self._nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    def challenge(self) -> str:
        nonce = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            for old in [key for key, expiry in self._nonces.items() if expiry <= now]:
                self._nonces.pop(old, None)
            if len(self._nonces) >= self.MAX_NONCES:
                oldest = min(self._nonces, key=lambda key: self._nonces[key])
                self._nonces.pop(oldest, None)
            self._nonces[nonce] = now + self.nonce_ttl
        return f'Digest realm="{self.realm}", nonce="{nonce}", algorithm=MD5, qop="auth"'

    def authenticate(self, header: str, method: str, uri: str) -> Principal | None:
        if not header[:7].lower() == "digest ":
            return None
        values = _digest_fields(header[7:])
        nonce = values.get("nonce", "")
        with self._lock:
            expiry = self._nonces.get(nonce, 0.0)
            if expiry < time.monotonic():
                self._nonces.pop(nonce, None)
                return None
        digest_uri = values.get("uri", "")
        principal = self.lookup(values.get("username", ""))
        if (
            principal is None
            or not digest_uri
            or not same_target(digest_uri, uri)
            or values.get("realm") != self.realm
            or values.get("algorithm", "MD5").upper() != "MD5"
        ):
            return None
        ha1 = hashlib.md5(f"{principal.username}:{self.realm}:{principal.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{digest_uri}".encode()).hexdigest()
        qop = values.get("qop", "")
        if qop and qop != "auth":
            return None
        if qop:
            if not values.get("nc") or not values.get("cnonce"):
                return None
            expected = hashlib.md5(
                f"{ha1}:{nonce}:{values['nc']}:{values['cnonce']}:{qop}:{ha2}".encode()
            ).hexdigest()
        else:
            expected = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        if not secrets.compare_digest(expected, values.get("response", "")):
            return None
        return principal
