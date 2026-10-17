"""Browser-facing HTTP security helpers shared by the legacy console and webapi.

Everything here is pure policy: response headers, cookie attributes, the Home
Assistant ingress prefix, and the login throttle.  Keeping it out of the SOAP
module lets the ONVIF transport stay small and lets the policy be unit tested
without a socket.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Final

# Strict policy for the static app shell and JSON API.  No inline script or
# style, no third-party origin, no framing.  ``SCRIPT_HASHES`` may add the
# sha256 of the app shell's inline import map (computed at runtime).
APP_CSP: Final = (
    "default-src 'self'; script-src 'self'{script_hashes}; style-src 'self'; "
    "img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors {frame_ancestors}"
)
# The legacy console is generated with inline script and style.  It keeps the
# same origin restrictions but cannot use a strict script-src until it is
# retired in favour of the app shell.
LEGACY_CSP: Final = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors {frame_ancestors}"
)

# Home Assistant's supervisor proxies ingress requests under this prefix and
# announces it in ``X-Ingress-Path``.  Accept only that exact shape so a
# client-chosen header cannot inject arbitrary text into redirects or pages.
_INGRESS_PATH: Final = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]{1,128}$")


def ingress_prefix(headers: Mapping[str, str] | object) -> str:
    """Return the validated ingress prefix (no trailing slash), or ``""``."""
    get = getattr(headers, "get", None)
    if get is None:
        return ""
    value = get("X-Ingress-Path", "") or ""
    value = str(value).strip().rstrip("/")
    return value if _INGRESS_PATH.fullmatch(value) else ""


def request_is_tls(headers: Mapping[str, str] | object) -> bool:
    """Whether the browser reached us over HTTPS (directly or via a proxy).

    Osprey itself listens on plain HTTP; HTTPS is terminated by a reverse
    proxy.  The forwarded headers are client-controlled, but they only ever
    *add* the ``Secure`` attribute, which cannot weaken a session.
    ``OSPREY_COOKIE_SECURE=1`` forces the attribute for proxies that do not
    send either header.
    """
    if os.environ.get("OSPREY_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"):
        return True
    get = getattr(headers, "get", None)
    if get is None:
        return False
    proto = str(get("X-Forwarded-Proto", "") or "").split(",", 1)[0].strip().lower()
    if proto == "https":
        return True
    forwarded = str(get("Forwarded", "") or "").lower()
    return bool(re.search(r"(^|[;,\s])proto=\"?https\"?", forwarded))


def session_cookie(name: str, value: str, *, secure: bool, max_age: int | None = None,
                   path: str = "/") -> str:
    """Render a session cookie: HttpOnly, SameSite=Strict, Secure on TLS.

    ``path`` scopes the cookie to the Home Assistant ingress prefix when the
    console is reached through ingress.
    """
    parts = [f"{name}={value}", "HttpOnly", "SameSite=Strict", f"Path={path}"]
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def security_headers(*, legacy: bool = False, framed: bool = False,
                     script_hashes: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Headers every browser-facing response carries.

    ``framed`` relaxes framing to same-origin for Home Assistant ingress,
    whose panel embeds the add-on UI in an iframe on the same origin.
    """
    ancestors = "'self'" if framed else "'none'"
    hashes = "".join(f" 'sha256-{value}'" for value in script_hashes)
    policy = (LEGACY_CSP if legacy else APP_CSP).format(
        frame_ancestors=ancestors, script_hashes=hashes
    )
    return [
        ("Content-Security-Policy", policy),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "SAMEORIGIN" if framed else "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ]


class LoginThrottle:
    """Bound password guessing and the cost of the deliberately slow hash.

    Each peer may fail ``free_failures`` times; after that it is locked out
    for an exponentially growing interval capped at ``max_lockout``.  A
    process-wide semaphore also caps concurrent PBKDF2 verifications so many
    clients cannot pin every CPU at once.
    """

    def __init__(self, *, free_failures: int = 5, base_lockout: float = 2.0,
                 max_lockout: float = 300.0, window: float = 900.0,
                 max_peers: int = 1024, concurrent: int = 2) -> None:
        self.free_failures = free_failures
        self.base_lockout = base_lockout
        self.max_lockout = max_lockout
        self.window = window
        self.max_peers = max_peers
        self._peers: OrderedDict[str, tuple[int, float, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._verifying = threading.BoundedSemaphore(concurrent)

    def retry_after(self, peer: str, now: float | None = None) -> float:
        """Seconds the peer must wait before another attempt (0 = allowed)."""
        current = time.monotonic() if now is None else now
        with self._lock:
            item = self._peers.get(peer)
            if item is None:
                return 0.0
            failures, last, locked_until = item
            if current - last > self.window:
                del self._peers[peer]
                return 0.0
            return max(0.0, locked_until - current)

    def acquire(self) -> bool:
        """Reserve one verification slot without blocking."""
        return self._verifying.acquire(blocking=False)

    def release(self) -> None:
        self._verifying.release()

    def failure(self, peer: str, now: float | None = None) -> float:
        """Record a failed attempt and return the resulting lockout seconds."""
        current = time.monotonic() if now is None else now
        with self._lock:
            failures, last, _ = self._peers.pop(peer, (0, current, 0.0))
            if current - last > self.window:
                failures = 0
            failures += 1
            lockout = 0.0
            if failures > self.free_failures:
                lockout = min(self.max_lockout,
                              self.base_lockout * (2 ** (failures - self.free_failures - 1)))
            self._peers[peer] = (failures, current, current + lockout)
            while len(self._peers) > self.max_peers:
                self._peers.popitem(last=False)
            return lockout

    def success(self, peer: str) -> None:
        with self._lock:
            self._peers.pop(peer, None)
