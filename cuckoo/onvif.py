"""ONVIF front end — SOAP over HTTP.

Clients ask here for what the camera is, which streams exist, where to fetch them,
and how to move the head. Everything is answered out of the device model, so this
module knows nothing about the camera's own protocol; the callables in `Backend`
are the only way it reaches the controller.

Scope is Profile S plus PTZ, Imaging and pull-point Events: enough for Home
Assistant, ONVIF Device Manager, Synology and friends. Not a general ONVIF
implementation — every verb here is one a real client actually sends.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import hashlib
import hmac
import os
import secrets
import base64
import re
from datetime import datetime, timezone
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, ClassVar, Final
from xml.etree import ElementTree

import discovery
import identity as device_identity
from media import BANDWIDTH_WINDOW
from model import PAN, TILT, ZOOM, Camera, Position, VideoTrack
from frigate_config import Store
from principals import (
    ANY_USER, ROLE_ADMIN, ROLE_EVENTS, ROLE_MEDIA, ROLE_PTZ, DigestVerifier, Principal,
    UserDirectory,
)
from snapshots import SnapshotCache
from logbuffer import BUFFER, LogBuffer
from setup_wizard import SetupError, SetupManager

ONVIF_PORT: Final = 8000
DEVICE_PATH: Final = "/onvif/device_service"
MEDIA_PATH: Final = "/onvif/media_service"
PTZ_PATH: Final = "/onvif/ptz_service"
IMAGING_PATH: Final = "/onvif/imaging_service"
EVENTS_PATH: Final = "/onvif/events_service"
MAX_SOAP_REQUEST_BYTES: Final = 256 * 1024
# An oversized body up to this size is drained (and discarded) before the 413
# so the client reliably receives the response instead of a connection reset.
MAX_DISCARD_BYTES: Final = 4 * MAX_SOAP_REQUEST_BYTES
DISCARD_TIMEOUT_SEC: Final = 5.0
SNAPSHOT_PATH: Final = "/snapshot/"
PREVIEW_PATH: Final = "/preview/"
CONTROL_STEP_PATH: Final = "/control/step"
CONTROL_HOME_PATH: Final = "/control/home"
CONTROL_PRESET_PATH: Final = "/control/preset"
CONTROL_ZOOM_PATH: Final = "/control/zoom"
CONTROL_RECOVER_PATH: Final = "/control/ptz/recover"
# Unauthenticated, read-only liveness for monitoring. It carries PTZ state and
# counters only: no MAC, address, preset name, or credential.
HEALTH_PATH: Final = "/healthz"
MANUAL_STEP_FRACTION: Final = 0.04
LOGIN_PATH: Final = "/login"
FRIGATE_API_PATH: Final = "/api/frigate"
LOGS_API_PATH: Final = "/api/logs"
SETUP_PATH: Final = "/setup"
SETUP_API_PATH: Final = "/api/setup"
SESSION_COOKIE: Final = "osprey_session"
CSRF_FIELD: Final = "csrf"


class AdminAuth:
    """Small, dependency-free browser auth boundary for the local operator UI.

    Password hashes use ``pbkdf2_sha256$iterations$salt$derived``.  ONVIF SOAP
    remains unauthenticated because cameras and NVRs cannot use this browser
    session; only the operator UI, preview, snapshots, and JSON controls use it.
    """

    SESSION_TTL_SEC: Final = 12 * 60 * 60
    MAX_SESSIONS: Final = 32

    def __init__(self, password_hash: str | None = None) -> None:
        self.password_hash = password_hash.strip() if password_hash else None
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.password_hash)

    @classmethod
    def from_environment(cls) -> "AdminAuth":
        encoded = os.environ.get("OSPREY_ADMIN_PASSWORD_HASH")
        password_file = os.environ.get("OSPREY_ADMIN_PASSWORD_FILE")
        if not encoded and password_file:
            try:
                with open(password_file, encoding="utf-8") as handle:
                    password = handle.read().strip()
            except OSError as exc:
                raise RuntimeError(f"cannot read OSPREY_ADMIN_PASSWORD_FILE: {exc}") from exc
            if password:
                encoded = cls.hash_password(password)
        if not encoded:
            log.warning(
                "operator console authentication is disabled; configure "
                "OSPREY_ADMIN_PASSWORD_HASH or OSPREY_ADMIN_PASSWORD_FILE"
            )
        return cls(encoded)

    @staticmethod
    def hash_password(password: str, iterations: int = 310_000) -> str:
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
        return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"

    def verify(self, password: str) -> bool:
        if not self.password_hash:
            return False
        try:
            scheme, count, salt, expected = self.password_hash.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            rounds = int(count)
            if not 100_000 <= rounds <= 2_000_000:
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds).hex()
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(actual, expected)

    def login(self, password: str) -> tuple[str, str] | None:
        if not self.verify(password):
            return None
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self._lock:
            now = time.time()
            self._drop_expired(now)
            if len(self._sessions) >= self.MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda key: self._sessions[key][1])
                del self._sessions[oldest]
            self._sessions[session] = (csrf, now + self.SESSION_TTL_SEC)
        return session, csrf

    def csrf(self, session: str) -> str | None:
        with self._lock:
            item = self._sessions.get(session)
            if item is None:
                return None
            if item[1] <= time.time():
                del self._sessions[session]
                return None
            return item[0]

    def logout(self, session: str) -> None:
        with self._lock:
            self._sessions.pop(session, None)

    def _drop_expired(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        for session, (_, expires) in list(self._sessions.items()):
            if expires <= current:
                del self._sessions[session]


DEFAULT_MAX_SKEW_SECONDS: Final = 300


@dataclass(frozen=True)
class AuthResult:
    """Outcome of checking one SOAP request's WS-Security header.

    ``anonymous`` means authentication is not configured at all (compatibility
    mode). Otherwise ``principal`` is set on success and ``error`` explains a
    failure; an error never reaches the client beyond ``ter:NotAuthorized``.
    """

    principal: Principal | None = None
    error: str | None = None
    anonymous: bool = False

    @property
    def ok(self) -> bool:
        return self.anonymous or self.principal is not None


class OnvifAuth:
    """Optional WS-Security UsernameToken verifier for one or more principals.

    Disabled when nothing is configured, preserving anonymous camera/Frigate
    behaviour. ``OnvifAuth(username, password)`` is the original single-credential
    form; that user holds every role. ``users`` adds principals with roles (see
    ``principals``). PasswordDigest is ``Base64(SHA1(nonce + created + password))``.

    ``Created`` must be within ``max_skew_seconds`` of Osprey's clock in either
    direction (default 300 s, ``OSPREY_ONVIF_MAX_SKEW_SECONDS``); clients that
    follow ONVIF call the pre-auth GetSystemDateAndTime first and correct for
    their own offset. Each accepted (nonce, created, username) is remembered for
    the skew window and a repeat is rejected.
    """

    # A client can submit a fresh nonce on every request. Keep replay
    # protection bounded so authenticated-but-untrusted clients cannot grow
    # this in-memory set without limit.
    MAX_REPLAY: ClassVar[int] = 4096

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
        *,
        users: UserDirectory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.username = username
        self.password = password
        self.max_skew_seconds = max_skew_seconds
        self.directory = users or UserDirectory()
        self._clock = clock
        self._replay: dict[bytes, float] = {}
        self._lock = threading.Lock()
        principals = list(self.directory.users)
        if username and password:
            if any(user.username == username for user in principals):
                raise RuntimeError(f"ONVIF user {username!r} is configured twice")
            principals.append(Principal(username, password, frozenset({ROLE_ADMIN})))
        self.principals: tuple[Principal, ...] = tuple(principals)

    @classmethod
    def from_environment(cls) -> "OnvifAuth":
        username = os.environ.get("OSPREY_ONVIF_USERNAME")
        password = os.environ.get("OSPREY_ONVIF_PASSWORD")
        path = os.environ.get("OSPREY_ONVIF_PASSWORD_FILE")
        if password is None and path:
            try:
                password = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise RuntimeError(f"cannot read OSPREY_ONVIF_PASSWORD_FILE: {exc}") from exc
        if bool(username) != bool(password):
            raise RuntimeError(
                "OSPREY_ONVIF_USERNAME and OSPREY_ONVIF_PASSWORD(_FILE) must be configured together"
            )
        users_file = os.environ.get("OSPREY_ONVIF_USERS_FILE")
        directory = None
        if users_file:
            try:
                directory = UserDirectory.load(users_file)
            except ValueError as exc:
                raise RuntimeError(f"invalid OSPREY_ONVIF_USERS_FILE: {exc}") from exc
        skew_text = os.environ.get("OSPREY_ONVIF_MAX_SKEW_SECONDS", "")
        skew = DEFAULT_MAX_SKEW_SECONDS
        if skew_text:
            try:
                skew = int(skew_text)
            except ValueError:
                skew = -1
            if not 30 <= skew <= 3600:
                raise RuntimeError("OSPREY_ONVIF_MAX_SKEW_SECONDS must be an integer from 30 to 3600")
        return cls(username or None, password or None, skew, users=directory)

    @property
    def configured(self) -> bool:
        """Whether an incomplete or complete authentication configuration exists."""
        return self.username is not None or self.password is not None or self.directory.enabled

    @property
    def enabled(self) -> bool:
        partial = bool(self.username) != bool(self.password)
        return bool(self.principals) and not partial

    def principal(self, username: str) -> Principal | None:
        return next((user for user in self.principals if user.username == username), None)

    def authenticate(self, payload: bytes, peer: str = "") -> AuthResult:
        if not self.configured:
            return AuthResult(anonymous=True)
        if not self.enabled:
            # A manually constructed partial config must fail closed.
            return AuthResult(error="authentication is misconfigured")
        try:
            root = ElementTree.fromstring(payload)
            token = next((e for e in root.iter() if local_name(e.tag) == "UsernameToken"), None)
            if token is None:
                return AuthResult(error="no UsernameToken")
            values = {local_name(e.tag): (e.text or "").strip() for e in token.iter()}
            nonce = base64.b64decode(values.get("Nonce", ""), validate=True)
            created = values.get("Created", "")
            digest = base64.b64decode(values.get("Password", ""), validate=True)
            if not 16 <= len(nonce) <= 64 or len(digest) != 20 or not created:
                return AuthResult(error="malformed UsernameToken")
            # ONVIF devices vary: both second precision and fractional
            # precision UTC timestamps are common.
            parsed = None
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    parsed = datetime.strptime(created, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return AuthResult(error="malformed Created timestamp")
            now = self._clock()
            if abs(now - parsed.timestamp()) > self.max_skew_seconds:
                return AuthResult(error="Created is outside the clock-skew window")
            username = values.get("Username", "")
            principal = self.principal(username)
            password = principal.password if principal is not None else ""
            expected = hashlib.sha1(nonce + created.encode() + password.encode()).digest()
            # Compare even for unknown users so timing does not reveal names.
            if not hmac.compare_digest(digest, expected) or principal is None:
                return AuthResult(error="invalid credentials")
            if peer and not principal.permits_peer(peer):
                return AuthResult(error="source address not allowed for this user")
            if not peer and principal.allow_from:
                return AuthResult(error="source address unknown")
            if principal.replay_protection:
                key = hashlib.sha256(nonce + created.encode() + username.encode()).digest()
                with self._lock:
                    for old, when in list(self._replay.items()):
                        if now - when > self.max_skew_seconds:
                            del self._replay[old]
                    if key in self._replay:
                        return AuthResult(error="replayed nonce")
                    if len(self._replay) >= self.MAX_REPLAY:
                        oldest = min(self._replay, key=lambda candidate: self._replay[candidate])
                        self._replay.pop(oldest, None)
                    self._replay[key] = now
            return AuthResult(principal=principal)
        except (ElementTree.ParseError, ValueError, TypeError, OverflowError):
            return AuthResult(error="unparseable security header")

    def valid(self, payload: bytes, peer: str = "") -> bool:
        """Backwards-compatible boolean form of :meth:`authenticate`."""
        return self.authenticate(payload, peer).ok


SOAP: Final = "http://www.w3.org/2003/05/soap-envelope"
NAMESPACES: Final = {
    "s": SOAP,
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "timg": "http://www.onvif.org/ver20/imaging/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "tns1": "http://www.onvif.org/ver10/topics",
    "ter": "http://www.onvif.org/ver10/error",
}

MANUFACTURER: Final = device_identity.DEFAULT_MANUFACTURER
PTZ_NODE: Final = "PTZNode"
VIDEO_SOURCE: Final = "VideoSource"
PTZ_CONFIG: Final = "PTZConfig"
FOV_TRANSLATION_SPACE: Final = (
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"
)

# The base generic PTZ spaces used by the node's SupportedPTZSpaces and by
# GetConfigurationOptions. _ptz_spaces inserts calibrated model alternatives in
# schema order. Home Assistant reads move-mode support from the
# *ConfigurationOptions* Spaces, not from the node — omit a space there and HA
# refuses that move mode ("RelativeMove not supported on device …") and the PTZ
# service silently no-ops. Absolute, Relative and Continuous are all advertised so
# every mode a client might send (arrow taps, hold-to-pan, presets) resolves; the
# camera itself has only preset-based motion, so each resolves to one relative step.
PTZ_SPACES: Final = (
    "<tt:AbsolutePanTiltPositionSpace>"
    "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace</tt:URI>"
    "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
    "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
    "</tt:AbsolutePanTiltPositionSpace>"
    "<tt:AbsoluteZoomPositionSpace>"
    "<tt:URI>http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace</tt:URI>"
    "<tt:XRange><tt:Min>0.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
    "</tt:AbsoluteZoomPositionSpace>"
    "<tt:RelativePanTiltTranslationSpace>"
    "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace</tt:URI>"
    "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
    "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
    "</tt:RelativePanTiltTranslationSpace>"
    "<tt:RelativeZoomTranslationSpace>"
    "<tt:URI>http://www.onvif.org/ver10/tptz/ZoomSpaces/TranslationGenericSpace</tt:URI>"
    "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
    "</tt:RelativeZoomTranslationSpace>"
    "<tt:ContinuousPanTiltVelocitySpace>"
    "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace</tt:URI>"
    "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
    "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
    "</tt:ContinuousPanTiltVelocitySpace>"
    "<tt:ContinuousZoomVelocitySpace>"
    "<tt:URI>http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace</tt:URI>"
    "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
    "</tt:ContinuousZoomVelocitySpace>"
    "<tt:PanTiltSpeedSpace>"
    "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace</tt:URI>"
    "<tt:XRange><tt:Min>0.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
    "</tt:PanTiltSpeedSpace>"
    "<tt:ZoomSpeedSpace>"
    "<tt:URI>http://www.onvif.org/ver10/tptz/ZoomSpaces/ZoomGenericSpeedSpace</tt:URI>"
    "<tt:XRange><tt:Min>0.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
    "</tt:ZoomSpeedSpace>"
)

FOV_RELATIVE_PAN_TILT_SPACE: Final = (
    "<tt:RelativePanTiltTranslationSpace>"
    f"<tt:URI>{FOV_TRANSLATION_SPACE}</tt:URI>"
    "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
    "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
    "</tt:RelativePanTiltTranslationSpace>"
)


def _ptz_spaces(camera: Camera | None) -> str:
    """Insert model-specific alternatives in the ONVIF schema's required sequence."""
    if camera is None or camera.field_of_view is None:
        return PTZ_SPACES
    end = "</tt:RelativePanTiltTranslationSpace>"
    return PTZ_SPACES.replace(end, end + FOV_RELATIVE_PAN_TILT_SPACE, 1)

# The Default*Space elements inside a PTZConfiguration. Home Assistant reads
# move-mode support ENTIRELY from these, off GetProfiles: relative from
# DefaultRelativePanTiltTranslationSpace, continuous from
# DefaultContinuousPanTiltVelocitySpace, absolute from
# DefaultAbsolutePantTiltPositionSpace. Emit only the absolute pair (as cuckoo
# first did) and HA marks the camera PTZ-capable but logs "RelativeMove not
# supported" and no-ops every relative/continuous move. Order matters — zeep
# validates the schema sequence and silently drops anything out of order. The
# "Pant" in DefaultAbsolutePantTiltPositionSpace is the ONVIF spec's own typo,
# which HA matches verbatim; keep it.
PTZ_DEFAULT_SPACES: Final = (
    "<tt:DefaultAbsolutePantTiltPositionSpace>"
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
    "</tt:DefaultAbsolutePantTiltPositionSpace>"
    "<tt:DefaultAbsoluteZoomPositionSpace>"
    "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"
    "</tt:DefaultAbsoluteZoomPositionSpace>"
    "<tt:DefaultRelativePanTiltTranslationSpace>"
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace"
    "</tt:DefaultRelativePanTiltTranslationSpace>"
    "<tt:DefaultRelativeZoomTranslationSpace>"
    "http://www.onvif.org/ver10/tptz/ZoomSpaces/TranslationGenericSpace"
    "</tt:DefaultRelativeZoomTranslationSpace>"
    "<tt:DefaultContinuousPanTiltVelocitySpace>"
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"
    "</tt:DefaultContinuousPanTiltVelocitySpace>"
    "<tt:DefaultContinuousZoomVelocitySpace>"
    "http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace"
    "</tt:DefaultContinuousZoomVelocitySpace>"
    "<tt:DefaultPTZTimeout>PT60S</tt:DefaultPTZTimeout>"
)
MOTION_TOPIC: Final = "tns1:RuleEngine/CellMotionDetector/Motion"
OBJECT_TOPIC: Final = "tns1:RuleEngine/MyRuleDetector"
AUDIO_TOPIC: Final = "tns1:AudioAnalytics/Audio/DetectedSound"

# The camera's object names, mapped onto the topics clients already listen for.
DETECTION_TOPICS: Final[dict[str, tuple[str, str]]] = {
    "person": (f"{OBJECT_TOPIC}/PeopleDetect", "IsPeople"),
    "vehicle": (f"{OBJECT_TOPIC}/VehicleDetect", "IsVehicle"),
    "animal": (f"{OBJECT_TOPIC}/DogCatDetect", "IsDogCat"),
    "package": (f"{OBJECT_TOPIC}/PackageDetect", "IsPackage"),
    "face": (f"{OBJECT_TOPIC}/FaceDetect", "IsFace"),
    "licensePlate": (f"{OBJECT_TOPIC}/LicensePlateDetect", "IsLicensePlate"),
}

log = logging.getLogger("cuckoo.onvif")

# ONVIF encoding names -> the codec strings the controller arms with.
ENCODING_TO_CODEC: dict[str, str] = {
    "H264": "h264", "H265": "h265", "HEVC": "h265", "JPEG": "mjpg", "MJPEG": "mjpg"
}


@dataclass
class Backend:
    """Everything the front end is allowed to ask of the controller."""

    camera: Callable[[], Camera | None]
    stream_uri: Callable[[str], str]
    snapshot_uri: Callable[[str], str]
    snapshot: Callable[[], bytes | None] = lambda: None
    move_absolute: Callable[[Position], bool] = lambda _p: False
    move_relative: Callable[[Position], bool] = lambda _p: False
    goto_preset: Callable[[int, int], bool] = lambda _i, _s: False
    set_preset: Callable[[str, int | None], int | None] = lambda _n, _i: None
    remove_preset: Callable[[int], bool] = lambda _i: False
    refresh_position: Callable[[], bool] = lambda: False
    # Halt the head: (pan_tilt, zoom) -> whether a stop was issued or not needed.
    stop: Callable[[bool, bool], bool] = lambda _pt, _z: False
    # Why a move cannot be dispatched right now, in the camera's own terms:
    # "PTZ channel unavailable" / "the camera is already moving" / generic.
    move_refusal: Callable[[], str] = lambda: "the camera is not accepting movement"
    # Re-arm a channel's codec live: (profile/config token, codec "h264"/"h265").
    set_encoder: Callable[[str, str], bool] = lambda _t, _c: False
    # Per-track runtime telemetry keyed by track name (see media.Hub.stats): the
    # windowed byte-rate + series, lifetime bytes, frames, subscribers, playable.
    telemetry: Callable[[], dict[str, dict[str, object]]] = dict
    # Recycle the PTZ channel through the controller's bounded recovery.
    recover_ptz: Callable[[], bool] = lambda: False
    # Machine-readable PTZ liveness (see Controller.ptz_health).
    ptz_health: Callable[[], dict[str, object]] = dict


# ------------------------------------------------------------------------ events


@dataclass
class Event:
    """One thing worth telling a subscriber about."""

    topic: str
    source: str
    name: str
    value: str
    at: float = field(default_factory=time.time)

    def as_xml(self) -> str:
        stamp = utc(self.at)
        return (
            "<wsnt:NotificationMessage>"
            f"<wsnt:Topic Dialect=\"http://docs.oasis-open.org/wsn/t-1/TopicExpression/Simple\">"
            f"{html.escape(self.topic)}</wsnt:Topic>"
            "<wsnt:Message>"
            f'<tt:Message UtcTime="{html.escape(stamp)}" PropertyOperation="Changed">'
            f'<tt:Source><tt:SimpleItem Name="Source" Value="{html.escape(self.source)}"/></tt:Source>'
            f'<tt:Data><tt:SimpleItem Name="{html.escape(self.name)}" Value="{html.escape(self.value)}"/></tt:Data>'
            "</tt:Message></wsnt:Message></wsnt:NotificationMessage>"
        )


@dataclass
class Subscription:
    owner: str
    expires: float
    queue: deque[Event]


class Subscriptions:
    """Pull-point subscriptions, each with its own bounded backlog and lifetime.

    Identifiers are unguessable. A subscription expires at its TerminationTime
    unless renewed (``Renew``) or pulled from (``PullMessages`` keeps a live
    subscriber alive, which matches what common clients expect). The table is
    capped globally and per owner: a client that leaks subscriptions (an NVR
    restarting, say) loses its own oldest one rather than starving others.
    """

    DEPTH: Final = 100
    MAX_TOTAL: Final = 16
    MAX_PER_OWNER: Final = 4
    DEFAULT_TTL: Final = 120.0
    MIN_TTL: Final = 10.0
    MAX_TTL: Final = 3600.0

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._items: dict[str, Subscription] = {}
        self._lock = threading.Lock()
        self._clock = clock

    @classmethod
    def clamp_ttl(cls, ttl: float | None) -> float:
        if ttl is None:
            return cls.DEFAULT_TTL
        return max(cls.MIN_TTL, min(cls.MAX_TTL, ttl))

    def _expire(self, now: float) -> None:
        for identifier in [key for key, item in self._items.items() if item.expires <= now]:
            del self._items[identifier]

    def create(self, owner: str = "", ttl: float | None = None) -> str | None:
        """A new subscription id, or None when the table is full."""
        with self._lock:
            now = self._clock()
            self._expire(now)
            mine = sorted(
                (key for key, item in self._items.items() if item.owner == owner),
                key=lambda key: self._items[key].expires,
            )
            while len(mine) >= self.MAX_PER_OWNER:
                del self._items[mine.pop(0)]
            if len(self._items) >= self.MAX_TOTAL:
                return None
            identifier = secrets.token_urlsafe(12)
            self._items[identifier] = Subscription(owner, now + self.clamp_ttl(ttl), deque(maxlen=self.DEPTH))
        return identifier

    def owner_of(self, identifier: str) -> str | None:
        with self._lock:
            self._expire(self._clock())
            item = self._items.get(identifier)
            return item.owner if item is not None else None

    def owned_by(self, owner: str) -> list[str]:
        with self._lock:
            self._expire(self._clock())
            return [key for key, item in self._items.items() if item.owner == owner]

    def expires(self, identifier: str) -> float | None:
        with self._lock:
            item = self._items.get(identifier)
            return item.expires if item is not None else None

    def renew(self, identifier: str, ttl: float | None = None) -> float | None:
        with self._lock:
            now = self._clock()
            self._expire(now)
            item = self._items.get(identifier)
            if item is None:
                return None
            item.expires = now + self.clamp_ttl(ttl)
            return item.expires

    def drop(self, identifier: str) -> bool:
        with self._lock:
            return self._items.pop(identifier, None) is not None

    def publish(self, event: Event) -> None:
        with self._lock:
            self._expire(self._clock())
            for item in self._items.values():
                item.queue.append(event)

    def pull(self, identifier: str, limit: int = 10) -> list[Event]:
        with self._lock:
            now = self._clock()
            self._expire(now)
            item = self._items.get(identifier)
            if item is None:
                return []
            item.expires = max(item.expires, now + self.DEFAULT_TTL)
            return [item.queue.popleft() for _ in range(min(limit, len(item.queue)))]

    def exists(self, identifier: str) -> bool:
        with self._lock:
            self._expire(self._clock())
            return identifier in self._items

    @property
    def count(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return len(self._items)


_DURATION_RE: Final = re.compile(
    r"P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?"
)


def parse_termination(text: str, now: float) -> float | None:
    """A WS-N TerminationTime (xs:duration or xs:dateTime) as seconds from now."""
    text = text.strip()
    if not text:
        return None
    match = _DURATION_RE.fullmatch(text)
    if match is not None and text not in ("P", "PT"):
        days, hours, minutes, seconds = (match.group(k) for k in "dhms")
        return (
            int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes or 0) * 60
            + float(seconds or 0)
        )
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            when = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        return when - now
    return None


def motion_event(camera: Camera, active: bool) -> Event:
    return Event(
        topic=MOTION_TOPIC,
        source=f"VideoSource_{camera.mac}",
        name="IsMotion",
        value="true" if active else "false",
    )


def detection_event(camera: Camera, kind: str, active: bool) -> Event:
    """Map one camera detection onto the topic a client is watching.

    Plain motion has a standard topic. Object detections do not, so they use the
    rule-detector topics clients already recognise, and anything unrecognised gets
    its own rule name rather than being dropped or mislabelled as motion.
    """
    if kind == "motion":
        return motion_event(camera, active)
    if kind.startswith("alrm") or kind == "audio":
        return Event(
            topic=AUDIO_TOPIC,
            source=f"AudioSource_{camera.mac}",
            name=kind if kind != "audio" else "IsSoundDetected",
            value="true" if active else "false",
        )
    topic, name = DETECTION_TOPICS.get(
        kind, (f"{OBJECT_TOPIC}/{kind[:1].upper()}{kind[1:]}Detect", f"Is{kind[:1].upper()}{kind[1:]}")
    )
    return Event(
        topic=topic,
        source=f"VideoSource_{camera.mac}",
        name=name,
        value="true" if active else "false",
    )


# -------------------------------------------------------------------------- SOAP


def utc(at: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at if at is not None else time.time()))


def envelope(body: str) -> str:
    declarations = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NAMESPACES.items())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<s:Envelope {declarations}><s:Body>{body}</s:Body></s:Envelope>"
    )


SENDER: Final = "s:Sender"
RECEIVER: Final = "s:Receiver"


def fault(reason: str, code: str = RECEIVER, subcodes: tuple[str, ...] = ()) -> str:
    """A SOAP 1.2 fault, optionally with ONVIF ``ter:`` subcodes (outermost first)."""
    nested = ""
    for subcode in reversed(subcodes):
        nested = f"<s:Subcode><s:Value>{subcode}</s:Value>{nested}</s:Subcode>"
    return envelope(
        f"<s:Fault><s:Code><s:Value>{code}</s:Value>{nested}</s:Code>"
        f"<s:Reason><s:Text xml:lang=\"en\">{html.escape(reason)}</s:Text></s:Reason></s:Fault>"
    )


def not_authorized(reason: str = "Sender not authorized") -> str:
    return fault(reason, SENDER, ("ter:NotAuthorized",))


def invalid_arg(reason: str, detail: str = "ter:InvalidArgVal") -> str:
    subcodes = ("ter:InvalidArgVal",) if detail == "ter:InvalidArgVal" else ("ter:InvalidArgVal", detail)
    return fault(reason, SENDER, subcodes)


def fault_status(body: str) -> HTTPStatus:
    """SOAP 1.2 HTTP binding: Sender faults are 400, Receiver faults are 500."""
    if "<s:Fault>" not in body:
        return HTTPStatus.OK
    if f"<s:Value>{SENDER}</s:Value>" in body:
        return HTTPStatus.BAD_REQUEST
    return HTTPStatus.INTERNAL_SERVER_ERROR


def local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


@dataclass
class Call:
    """A parsed SOAP request: which verb, and the body element to read from."""

    action: str
    body: ElementTree.Element
    namespace: str = ""

    def find(self, name: str) -> ElementTree.Element | None:
        for element in self.body.iter():
            if local_name(element.tag) == name:
                return element
        return None

    def text(self, name: str, default: str = "") -> str:
        element = self.find(name)
        if element is None or element.text is None:
            return default
        return element.text.strip()

    def attribute(self, element_name: str, attribute: str, default: str = "") -> str:
        element = self.find(element_name)
        if element is None:
            return default
        return element.attrib.get(attribute, default)

    def vector(self, element_name: str) -> tuple[float, float] | None:
        """PanTilt and Zoom arrive as x/y attributes on their own element."""
        element = self.find(element_name)
        if element is None:
            return None
        try:
            return float(element.attrib.get("x", "0")), float(element.attrib.get("y", "0"))
        except ValueError:
            return None


def parse_call(payload: bytes) -> Call | None:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return None
    body = root.find(f"{{{SOAP}}}Body")
    if body is None or len(body) == 0:
        return None
    first = body[0]
    namespace = first.tag[1:].partition("}")[0] if first.tag.startswith("{") else ""
    return Call(action=local_name(first.tag), body=first, namespace=namespace)


# ------------------------------------------------------------- operations

TDS: Final = NAMESPACES["tds"]
TRT: Final = NAMESPACES["trt"]
TPTZ: Final = NAMESPACES["tptz"]
TIMG: Final = NAMESPACES["timg"]
TEV: Final = NAMESPACES["tev"]
WSNT: Final = NAMESPACES["wsnt"]
SERVICE_NAMESPACES: Final = frozenset({TDS, TRT, TPTZ, TIMG, TEV, WSNT})
PRE_AUTH: Final = None


@dataclass(frozen=True)
class Operation:
    """One SOAP operation Osprey implements, and who may call it.

    ``role`` is ``PRE_AUTH`` (``None``) for the operations ONVIF Core lets any
    client call before authenticating (GetSystemDateAndTime, GetCapabilities,
    GetServices, GetServiceCapabilities, GetWsdlUrl, GetEndpointReference),
    ``ANY_USER`` for device metadata every authenticated client needs, or a
    role from ``principals``.
    """

    namespace: str
    action: str
    handler: str
    role: str | None


def _ops(namespace: str, role: str | None, *actions: str) -> list[Operation]:
    return [Operation(namespace, action, f"_{_snake(action)}", role) for action in actions]


def _snake(action: str) -> str:
    """CamelCase to snake_case, keeping acronyms whole (GetDNS -> get_dns)."""
    out: list[str] = []
    for index, character in enumerate(action):
        if character.isupper() and index:
            previous = action[index - 1]
            following = action[index + 1] if index + 1 < len(action) else ""
            if previous.islower() or (previous.isupper() and following.islower()):
                out.append("_")
        out.append(character.lower())
    return "".join(out)


# The complete allow-list. Nothing outside it is dispatched, whatever the
# request's element name; adding an operation means adding it here.
OPERATIONS: Final[tuple[Operation, ...]] = tuple(
    _ops(TDS, PRE_AUTH, "GetSystemDateAndTime", "GetCapabilities", "GetServices",
         "GetWsdlUrl", "GetEndpointReference")
    + [Operation(TDS, "GetServiceCapabilities", "_get_service_capabilities", PRE_AUTH)]
    + _ops(TDS, ANY_USER, "GetDeviceInformation", "GetScopes", "GetHostname", "GetDNS",
           "GetNTP", "GetNetworkInterfaces", "GetNetworkProtocols",
           "GetNetworkDefaultGateway", "GetDiscoveryMode")
    + _ops(TDS, ROLE_ADMIN, "SetSystemDateAndTime")
    + [Operation(TRT, "GetServiceCapabilities", "_get_media_service_capabilities", PRE_AUTH)]
    + _ops(TRT, ROLE_MEDIA, "GetProfiles", "GetProfile", "GetVideoSources",
           "GetVideoSourceConfigurations", "GetVideoSourceConfiguration",
           "GetCompatibleVideoSourceConfigurations", "GetVideoEncoderConfigurations",
           "GetVideoEncoderConfiguration", "GetCompatibleVideoEncoderConfigurations",
           "GetVideoEncoderConfigurationOptions", "GetGuaranteedNumberOfVideoEncoderInstances",
           "GetStreamUri", "GetSnapshotUri", "GetAudioSources", "GetAudioSourceConfigurations",
           "GetAudioEncoderConfigurations", "GetAudioOutputs", "SetSynchronizationPoint")
    + _ops(TRT, ROLE_ADMIN, "SetVideoEncoderConfiguration")
    + [Operation(TPTZ, "GetServiceCapabilities", "_get_ptz_service_capabilities", PRE_AUTH)]
    + _ops(TPTZ, ROLE_PTZ, "GetNodes", "GetNode", "GetConfigurations", "GetConfiguration",
           "GetConfigurationOptions", "GetStatus", "GetPresets", "AbsoluteMove",
           "RelativeMove", "ContinuousMove", "Stop", "GotoPreset", "SetPreset", "RemovePreset")
    + [Operation(TIMG, "GetServiceCapabilities", "_get_imaging_service_capabilities", PRE_AUTH)]
    + _ops(TIMG, ROLE_MEDIA, "GetImagingSettings", "GetOptions")
    + [Operation(TEV, "GetServiceCapabilities", "_get_events_service_capabilities", PRE_AUTH)]
    + _ops(TEV, ROLE_EVENTS, "CreatePullPointSubscription", "PullMessages",
           "GetEventProperties")
    + [Operation(TEV, "SetSynchronizationPoint", "_set_event_synchronization_point", ROLE_EVENTS)]
    + _ops(WSNT, ROLE_EVENTS, "Renew", "Unsubscribe")
)
_BY_KEY: Final[dict[tuple[str, str], Operation]] = {(op.namespace, op.action): op for op in OPERATIONS}
_BY_ACTION: Final[dict[str, Operation]] = {}
for _operation in OPERATIONS:
    _BY_ACTION.setdefault(_operation.action, _operation)


def operation_for(call: Call) -> Operation | None:
    """Resolve a call against the allow-list.

    A request in a known ONVIF service namespace must match that service
    exactly (an Imaging ``Stop`` is not a PTZ ``Stop``). Some clients put
    operations in other namespaces (or tests use a placeholder); those resolve
    by action name to the first service that defines it.
    """
    if call.namespace in SERVICE_NAMESPACES:
        found = _BY_KEY.get((call.namespace, call.action))
        if found is None and call.namespace == TEV and call.action in ("Renew", "Unsubscribe"):
            found = _BY_KEY.get((WSNT, call.action))
        return found
    return _BY_ACTION.get(call.action)


@dataclass(frozen=True)
class Caller:
    """Who is making the current SOAP request (set for the call's duration)."""

    principal: Principal | None = None
    anonymous: bool = True
    peer: str = ""
    subscription: str = ""

    def may(self, role: str) -> bool:
        if self.principal is None:
            return True
        return self.principal.has(role)

    @property
    def owner(self) -> str:
        if self.principal is not None:
            return f"user:{self.principal.username}"
        return f"peer:{self.peer}"


SNAPSHOT_REALM: Final = "Osprey ONVIF"
SNAPSHOT_TOKEN_PARAM: Final = "auth"


# ---------------------------------------------------------------------- services


class Services:
    """Turns parsed calls into SOAP responses, using only the device model."""

    def __init__(self, backend: Backend, host: str, port: int = ONVIF_PORT,
                 auth: AdminAuth | None = None, onvif_auth: OnvifAuth | None = None,
        read_only: bool = False, logs: LogBuffer | None = None,
        setup: SetupManager | None = None,
        frigate_url: str | None = None,
        identity: device_identity.IdentityConfig | None = None,
        name: str = "osprey",
        interface_mac: str | None = None,
        extra_scopes: tuple[str, ...] = (),
        snapshot_interval: float = 1.0) -> None:
        self.backend = backend
        self.host = host
        self.port = port
        self.subscriptions = Subscriptions()
        self.started_at = time.time()
        self.auth = auth or AdminAuth()
        self.onvif_auth = onvif_auth or OnvifAuth()
        self.read_only = read_only
        self.logs = logs or BUFFER
        self.setup = setup
        self.frigate_store = Store.from_environment()
        self.frigate_url = self.frigate_store.load() or frigate_url
        self.identity_config = identity or device_identity.IdentityConfig()
        self.name = name
        self.interface_mac = interface_mac
        self.extra_scopes = extra_scopes
        self.snapshots = SnapshotCache(lambda: self.backend.snapshot(), snapshot_interval)
        self._caller = threading.local()
        # Capability tokens in snapshot URIs are HMACs under a per-process key,
        # so they become invalid on restart (InvalidAfterReboot=true).
        self._snapshot_key = secrets.token_bytes(32)
        self._snapshot_digest = DigestVerifier(SNAPSHOT_REALM, self.onvif_auth_principal)

    # ---------------------------------------------------------------- identity

    def onvif_auth_principal(self, username: str) -> Principal | None:
        return self.onvif_auth.principal(username)

    @property
    def caller(self) -> Caller:
        current = getattr(self._caller, "value", None)
        return current if isinstance(current, Caller) else Caller()

    def device_identity(self) -> device_identity.DeviceIdentity:
        camera = self.backend.camera()
        seed = camera.mac if camera is not None and camera.mac else f"{self.host}:{self.port}"
        return device_identity.resolve(
            self.identity_config,
            seed=seed,
            camera_mac=camera.mac if camera is not None else None,
            camera_model=camera.model if camera is not None and camera.model else None,
            controller_name=self.name,
            interface_hw=self.interface_mac,
        )

    def scopes(self) -> tuple[str, ...]:
        camera = self.backend.camera()
        name = camera.name if camera is not None and camera.name else self.name
        return discovery.scopes_for(
            name, self.ptz_enabled, self.device_identity().model, self.extra_scopes
        )

    def frigate_settings(self) -> dict[str, object]:
        return {"base_url": self.frigate_url, "configured": self.frigate_url is not None}

    def log_entries(self, limit: int = 100, minimum: int = logging.INFO) -> dict[str, object]:
        """Return recent, redacted records for the authenticated console."""
        entries = self.logs.entries(limit, minimum)
        summary = self.logs.summary()
        return {
            "entries": entries,
            "warnings": summary["warnings"],
            "errors": summary["errors"],
        }

    def set_frigate_url(self, value: object) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(value, str):
            return HTTPStatus.BAD_REQUEST, {"error": "base_url must be a string"}
        try:
            self.frigate_url = self.frigate_store.save(value)
        except ValueError as exc:
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        return HTTPStatus.OK, self.frigate_settings()

    # ------------------------------------------------------------- addressing

    def address(self, path: str) -> str:
        return f"http://{html.escape(self.host, quote=True)}:{self.port}{html.escape(path, quote=True)}"

    @property
    def ptz_enabled(self) -> bool:
        """Whether this ONVIF persona exposes PTZ to the current caller.

        A principal without the ``ptz`` role (UniFi Protect, typically) sees the
        device exactly as the read-only persona does: no PTZ service, no PTZ
        configuration in profiles.
        """
        camera = self.backend.camera()
        return (
            not self.read_only and camera is not None and camera.is_ptz
            and self.caller.may(ROLE_PTZ)
        )

    # ------------------------------------------------------------ local control
    def control_step(self, data: object) -> tuple[HTTPStatus, dict[str, object]]:
        """Apply one deliberately small, validated PTZ step from the local UI."""
        if not isinstance(data, dict) or set(data) - {"axis", "direction", "step"} or not {"axis", "direction"} <= set(data):
            return HTTPStatus.BAD_REQUEST, {"error": "expected axis and direction"}
        axis, direction = data["axis"], data["direction"]
        step = data.get("step", round(MANUAL_STEP_FRACTION * 100))
        if not isinstance(axis, str) or not isinstance(direction, int) or isinstance(direction, bool) or axis not in (PAN, TILT, ZOOM) or direction not in (-1, 1) or not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= 20:
            return HTTPStatus.BAD_REQUEST, {"error": "axis or direction is invalid"}
        camera = self.backend.camera()
        if camera is None or not camera.is_ptz:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no PTZ camera"}
        current = camera.motion.position
        ranges = {PAN: camera.pan_range, TILT: camera.tilt_range, ZOOM: camera.zoom_range}
        values = current.as_dict()
        span = ranges[axis].maximum - ranges[axis].minimum
        # G5 raw tilt coordinates increase as the head tilts down.  The local
        # UI speaks physical directions, so a positive direction must be up.
        sign = -1 if axis == TILT else 1
        values[axis] = ranges[axis].clamp(values[axis] + round(span * (step / 100) * direction * sign))
        if not self.backend.move_relative(Position(**values)):
            return HTTPStatus.CONFLICT, {"error": self.backend.move_refusal()}
        return HTTPStatus.OK, {"ok": True, "axis": axis, "direction": direction}

    def control_zoom(self, data: object) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(data, dict) or set(data) != {"percent"} or not isinstance(data["percent"], int) or isinstance(data["percent"], bool) or not 0 <= data["percent"] <= 100:
            return HTTPStatus.BAD_REQUEST, {"error": "percent must be an integer from 0 to 100"}
        camera = self.backend.camera()
        if camera is None or not camera.is_ptz:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no PTZ camera"}
        percent = data["percent"]
        target = camera.zoom_range.from_normalised(percent / 50.0 - 1.0)
        values = camera.motion.position.as_dict()
        values[ZOOM] = target
        if not self.backend.move_absolute(Position(**values)):
            return HTTPStatus.CONFLICT, {"error": self.backend.move_refusal()}
        return HTTPStatus.OK, {"ok": True, "percent": percent}

    def control_home(self) -> tuple[HTTPStatus, dict[str, object]]:
        camera = self.backend.camera()
        if camera is None or not camera.is_ptz:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no PTZ camera"}
        if camera.motion.status != "IDLE":
            return HTTPStatus.CONFLICT, {"error": "wait for movement to finish before saving home"}
        home = next((p for p in camera.presets.values() if p.name.strip().lower() == "home"), None)
        updated = home is not None
        existing_home_token = home.index if home is not None else None
        assigned = self.backend.set_preset("home", existing_home_token)
        if assigned is None:
            return HTTPStatus.CONFLICT, {"error": "the camera is not accepting presets"}
        return HTTPStatus.OK, {
            "ok": True,
            "token": assigned,
            "updated": updated,
            "message": "Home position updated" if updated else "Home position saved",
        }

    def control_preset(self, data: object) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(data, dict) or set(data) != {"action", "name"}:
            return HTTPStatus.BAD_REQUEST, {"error": "expected action and name"}
        action, name = data["action"], data["name"]
        if action not in ("save", "goto") or not isinstance(name, str) or not 1 <= len(name) <= 64 or not name.strip():
            return HTTPStatus.BAD_REQUEST, {"error": "action or name is invalid"}
        camera = self.backend.camera()
        if camera is None or not camera.is_ptz:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no PTZ camera"}
        existing = next((p for p in camera.presets.values() if p.name.casefold() == name.casefold()), None)
        if action == "save":
            if camera.motion.status != "IDLE":
                return HTTPStatus.CONFLICT, {"error": "wait for movement to finish before saving a preset"}
            # A case-insensitive match is the same user-facing position. Keep
            # its established spelling while replacing the camera token so
            # callers do not accidentally manufacture ``Gate``/``gate`` twins.
            canonical_name = existing.name if existing is not None else name
            token = self.backend.set_preset(
                canonical_name, existing.index if existing else None
            )
            if token is None:
                return HTTPStatus.CONFLICT, {"error": "the camera is not accepting presets"}
            return HTTPStatus.OK, {
                "ok": True,
                "action": action,
                "name": canonical_name,
                "token": token,
            }
        if existing is None:
            return HTTPStatus.NOT_FOUND, {"error": "no such named preset"}
        if not self.backend.goto_preset(existing.index, 1000):
            return HTTPStatus.CONFLICT, {"error": self.backend.move_refusal()}
        return HTTPStatus.OK, {"ok": True, "action": action, "name": existing.name, "token": existing.index}

    def control_recover(self) -> tuple[HTTPStatus, dict[str, object]]:
        camera = self.backend.camera()
        if camera is None or not camera.is_ptz:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no PTZ camera"}
        if not self.backend.recover_ptz():
            return HTTPStatus.CONFLICT, {"error": "the camera management channel is not connected"}
        return HTTPStatus.ACCEPTED, {
            "ok": True,
            "message": "PTZ recovery started; state returns once the camera answers",
        }

    def health(self) -> tuple[HTTPStatus, dict[str, object]]:
        """Liveness for monitoring: 200 when usable, 503 when PTZ is degraded."""
        camera = self.backend.camera()
        if camera is None:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"status": "waiting", "camera": False}
        body: dict[str, object] = {
            "status": "ok",
            "camera": True,
            "adopted": camera.adopted,
            "uptime_sec": int(time.time() - self.started_at),
        }
        if camera.is_ptz and not self.read_only:
            ptz_state = self.backend.ptz_health()
            body["ptz"] = ptz_state
            if not ptz_state.get("healthy"):
                body["status"] = "degraded"
                return HTTPStatus.SERVICE_UNAVAILABLE, body
        return HTTPStatus.OK, body

    def control_codec(self, data: object) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(data, dict) or set(data) != {"track", "codec"}:
            return HTTPStatus.BAD_REQUEST, {"error": "expected track and codec"}
        track_name, codec = data["track"], data["codec"]
        camera = self.backend.camera()
        if not isinstance(track_name, str) or not isinstance(codec, str) or codec.lower() not in ("h264", "h265", "mjpg"):
            return HTTPStatus.BAD_REQUEST, {"error": "track or codec is invalid"}
        if camera is None or camera.track(track_name) is None:
            return HTTPStatus.NOT_FOUND, {"error": "no such track"}
        if not self.backend.set_encoder(track_name, codec.lower()):
            return HTTPStatus.CONFLICT, {"error": "codec change was refused"}
        return HTTPStatus.OK, {"ok": True, "track": track_name, "codec": codec.lower()}

    # ------------------------------------------------------------- telemetry

    def status_page(self) -> str:
        """A human-facing telemetry page served at GET / on the ONVIF port.

        Live controller state: the adopted camera and PTZ position, per-track
        ingest bandwidth (rate, rolling-window sparkline, lifetime bytes, frames,
        RTSP subscribers), presets, and the endpoints a client points at.
        """
        camera = self.backend.camera()
        tele = self.backend.telemetry()

        def esc(value: object) -> str:
            return html.escape(str(value))

        def rows(*pairs: tuple[str, str]) -> str:
            return "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in pairs)

        def num(stat: dict[str, object], key: str) -> float:
            value = stat.get(key, 0)
            return float(value) if isinstance(value, (int, float)) else 0.0

        def series_of(stat: dict[str, object]) -> list[float]:
            value = stat.get("series")
            return [float(v) for v in value] if isinstance(value, list) else []

        up = int(time.time() - self.started_at)
        days, rem = divmod(up, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        uptime = (f"{days}d " if days else "") + f"{hours:02d}:{mins:02d}:{secs:02d}"

        frigate = self.frigate_settings()
        frigate_url = esc(frigate["base_url"] or "")
        if camera is None:
            counts = self.logs.summary()
            alert = f"{counts['errors']} error(s), {counts['warnings']} warning(s) in recent log"
            body = (
                "<section><h2>Camera</h2><p class='muted'>No camera adopted yet.</p></section>"
                "<section class='span'><div class='section-head'><div><h2>Logs &amp; alerts</h2>"
                "<p class='muted'>Recent Osprey records; the host journal remains the full log.</p></div>"
                f"<strong id='log-alert' class='alert-warning'>{esc(alert)}</strong></div>"
                "<pre id='log-output' class='log-output' aria-live='polite'>Loading recent logs…</pre></section>"
            )
            return (
                "<!doctype html><html lang='en'><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Osprey · operator console</title></head><body><main><h1>Osprey operator console</h1>"
                f"<p>Controller state: waiting · {esc(self.host)}:{self.port}</p><div>{body}</div>"
                "<script>async function loadLogs(){const out=document.getElementById('log-output');"
                "try{const response=await fetch('/api/logs?limit=100');const data=await response.json();"
                "if(!response.ok)throw new Error(data.error||'request failed');out.textContent=data.entries.map(e=>`[${e.timestamp}] ${e.level} ${e.logger}: ${e.message}`).join('\\n')||'No records yet';"
                "document.getElementById('log-alert').textContent=`${data.errors} error(s), ${data.warnings} warning(s) in recent log`;"
                "}catch(error){out.textContent='Unable to load logs: '+error}}loadLogs();setInterval(loadLogs,5000);</script>"
                "</main></body></html>"
            )
        else:
            pos = camera.motion.position
            pan = camera.pan_range.to_normalised(pos.pan)
            tilt = -camera.tilt_range.to_normalised(pos.tilt)  # ONVIF +Y = up
            zoom = (camera.zoom_range.to_normalised(pos.zoom) + 1.0) / 2.0
            zoom_percent = round(zoom * 100)
            cam = rows(
                ("MAC", esc(camera.mac)),
                ("Model", esc(camera.model or "—")),
                ("Firmware", esc(camera.firmware or "—")),
                ("PTZ", "yes" if camera.is_ptz else "no"),
                ("Motion", esc(camera.motion.status.lower()) + (
                    f" · <span class='alert-danger'>{esc(camera.motion.error)}</span>"
                    if camera.motion.error else "")),
                ("Audio", esc(", ".join(c.value for c in camera.audio_codecs) or "—")),
            )
            if camera.is_ptz:
                cam += rows(
                    ("Position (ONVIF)", f"pan {pan:+.3f} · tilt {tilt:+.3f} · zoom {zoom:.3f}"),
                    ("Position (motor)", f"pan {pos.pan} · tilt {pos.tilt} · zoom {pos.zoom}"),
                    ("Pan / tilt range",
                     f"{camera.pan_range.minimum}‥{camera.pan_range.maximum} · "
                     f"{camera.tilt_range.minimum}‥{camera.tilt_range.maximum}"),
                )
            camera_section = f"<section><h2>Camera</h2><table class='kv'>{cam}</table></section>"

            # Keep the overview useful even when Frigate is not reachable: every
            # value here is sourced from the local media/controller state.
            playable_count = sum(1 for stat in tele.values() if bool(stat.get("playable")))
            total_rate = sum(num(stat, "rate_bps") for stat in tele.values())
            total_frames = sum(num(stat, "frames") for stat in tele.values())
            # The headline must be the ONVIF state Frigate sees, never a
            # derived "idle" that hides UNKNOWN (channel lost, motor timeout).
            ptz_state = camera.motion.status
            ptz_error = camera.motion.error
            ptz_detail = ptz_error or ("ready" if ptz_state == "IDLE" else "in motion")
            ptz_class = " class='alert-danger'" if ptz_state == "UNKNOWN" else ""
            metric_cards = (
                f"<div class='metric-grid'>"
                f"<div class='metric' id='ptz-state-card'><span>PTZ state</span><strong{ptz_class}>{esc(ptz_state)}</strong><small id='ptz-error'>{esc(ptz_detail)}</small></div>"
                f"<div class='metric'><span>Position</span><strong>{pan:+.2f} / {tilt:+.2f}</strong><small>pan / tilt · ONVIF units</small></div>"
                f"<div class='metric'><span>Zoom</span><strong>{zoom_percent}%</strong><small>absolute target</small></div>"
                f"<div class='metric'><span>Streams online</span><strong>{playable_count} / {len(camera.tracks)}</strong><small>{_fmt_rate(total_rate)} in · {int(total_frames)} frames</small></div>"
                f"</div>"
            )
            metrics = f"<section class='span'><div class='section-head'><div><h2>Mission overview</h2><p class='muted'>Live controller and ingest telemetry · refresh the page for a new snapshot</p></div><span class='live-dot'>● LIVE</span></div>{metric_cards}</section>"

            image = self.backend.snapshot()
            # A configured-but-idle encoder has no frames for ffmpeg to decode.
            # Prefer the stream Hub has actually marked playable, rather than
            # blindly choosing the first advertised profile.
            preview_track = next(
                (
                    track
                    for track in camera.tracks
                    if bool(tele.get(track.name, {}).get("playable"))
                ),
                None,
            )
            if preview_track is not None:
                live_section = (
                    f"<section><div class='section-head'><h2>Live video</h2><span id='preview-state' class='muted'>Connecting…</span></div><img src='{PREVIEW_PATH}{esc(preview_track.name)}' alt='live video preview' onload=\"document.getElementById('preview-state').textContent='Stream online'\" onerror=\"document.getElementById('preview-state').textContent='Stream unavailable';this.hidden=true;document.getElementById('snapshot-fallback-label')?.removeAttribute('hidden');document.getElementById('snapshot-fallback')?.removeAttribute('hidden')\">"
                    + (f"<p id='snapshot-fallback-label' class='muted' hidden>Snapshot fallback</p><img id='snapshot-fallback' hidden src='{SNAPSHOT_PATH}?t={up}' alt='snapshot'>" if image else "")
                    + "</section>"
                )
            elif image:
                live_section = f"<section><div class='section-head'><h2>Live snapshot</h2><span class='muted'>Fallback feed</span></div><img id='snapshot-fallback' src='{SNAPSHOT_PATH}?t={up}' alt='snapshot' onerror=\"this.alt='Snapshot unavailable';this.classList.add('media-error')\"></section>"
            else:
                live_section = "<section><h2>Live video</h2><p class='muted'>Waiting for a playable camera stream.</p></section>"

            track_rows = ""
            for track in camera.tracks:
                stat = tele.get(track.name, {})
                playable = bool(stat.get("playable"))
                dot = "ok" if playable else "warn"
                track_rows += (
                    "<tr>"
                    f"<td><span class='dot {dot}'></span>{esc(track.name)}</td>"
                    f"<td>{track.width}×{track.height}</td>"
                    f"<td>{esc(track.codec.value)}</td>"
                    f"<td class='num'>{esc(_fmt_rate(num(stat, 'rate_bps')))}</td>"
                    f"<td class='spark-cell'>{_sparkline(series_of(stat))}</td>"
                    f"<td class='num'>{esc(_fmt_bytes(num(stat, 'bytes_in')))}</td>"
                    f"<td class='num'>{int(num(stat, 'frames'))} "
                    f"<span class='muted'>/ {int(num(stat, 'keyframes'))} kf</span></td>"
                    f"<td class='num'>{int(num(stat, 'subscribers'))}</td>"
                    "</tr>"
                )
            bandwidth = (
                "<section class='span'><h2>Streams &amp; bandwidth</h2><div class='scroll'>"
                "<table class='wide'><tr><th>Track</th><th>Resolution</th><th>Codec</th>"
                f"<th class='num'>Rate</th><th>Last {int(BANDWIDTH_WINDOW)}s</th>"
                "<th class='num'>Total</th><th class='num'>Frames</th>"
                f"<th class='num'>Subs</th></tr>{track_rows}</table></div></section>"
            )

            if camera.presets:
                preset_rows = "".join(
                    f"<tr><th>{i}</th><td>{esc(p.name)}</td><td><button type='button' data-goto-preset='{esc(p.name)}'>Go to</button></td></tr>"
                    for i, p in sorted(camera.presets.items())
                )
                presets = (f"<section><h2>Named positions</h2><table class='kv'>{preset_rows}</table>"
                           "<p class='muted'>Use named positions for operators. <code>home</code> remains the current Frigate return target.</p>"
                           "<label>Save current view as <input id='preset-name' maxlength='64' required></label> "
                           "<button type='button' id='save-preset'>Save named position</button></section>")
            else:
                presets = ("<section><h2>Named positions</h2><p class='muted'>None set. "
                           "<code>home</code> remains the current Frigate return target.</p>"
                           "<label>Save current view as <input id='preset-name' maxlength='64' required></label> "
                           "<button type='button' id='save-preset'>Save named position</button></section>")

            endpoint_rows = rows(
                ("ONVIF", f"<span class='mono'>{esc(self.address(DEVICE_PATH))}</span>"),
                ("Snapshot", f"<span class='mono'>{esc(self.address(SNAPSHOT_PATH))}</span>"),
            ) + "".join(
                f"<tr><th>RTSP {esc(t.name)}</th>"
                f"<td><span class='mono'>{esc(self.backend.stream_uri(t.name))}</span></td></tr>"
                for t in camera.tracks
            )
            endpoints = (f"<section class='span'><h2>Endpoints</h2><table class='kv'>{endpoint_rows}</table>"
                         "<p class='muted'>RTSP video/audio links are for players and NVRs; browsers generally cannot play RTSP directly. Use the snapshot above for browser viewing.</p></section>")

            controls = ""
            if camera.is_ptz:
                controls = ("<section><h2>PTZ controls</h2><div class='controls'>"
                    "<button type='button' data-axis='pan' data-direction='-1'>← Pan</button>"
                    "<button type='button' data-axis='tilt' data-direction='1'>↑ Tilt</button>"
                    "<button type='button' data-axis='tilt' data-direction='-1'>↓ Tilt</button>"
                    "<button type='button' data-axis='pan' data-direction='1'>Pan →</button>"
                    "<label>Step <input id='step-size' type='range' min='1' max='20' value='4' aria-label='PTZ step size'> <output id='step-value'>4%</output></label>"
                    f"<label>Zoom <input id='zoom-level' type='range' min='0' max='100' value='{zoom_percent}' aria-label='Absolute zoom percentage'> <output id='zoom-value'>{zoom_percent}%</output></label>"
                    "<button type='button' id='save-home'>Save current view as home</button>"
                    "<button type='button' id='recover-ptz' class='danger'>Recover PTZ</button></div>"
                    "<p id='feedback' role='status' aria-live='polite'></p>"
                    "<p class='muted'>Steps are 4% of the configured range. Saving creates or replaces Home. "
                    "Recover PTZ recycles the camera's PTZ channel when the state is stuck at UNKNOWN.</p></section>")
            codec_controls = "".join(
                f"<label>{esc(t.name)} codec <select data-track='{esc(t.name)}' aria-label='{esc(t.name)} codec'>"
                + "".join(f"<option value='{c}' {'selected' if c == t.codec.value else ''}>{c}</option>" for c in ('h264', 'h265', 'mjpg'))
                + "</select></label>"
                for t in camera.tracks
            )
            settings = (f"<section><h2>Supported settings</h2><div class='controls'>{codec_controls}</div>"
                        "<p class='muted'>Codec changes are applied by the active encoder backend.</p></section>" if codec_controls else "")

            log_counts = self.logs.summary()
            alert_class = "alert-danger" if log_counts["errors"] else ("alert-warning" if log_counts["warnings"] else "alert-ok")
            alert_text = (
                f"{log_counts['errors']} error(s), {log_counts['warnings']} warning(s) in the recent log"
                if log_counts["errors"] or log_counts["warnings"]
                else "No warnings in the recent log"
            )
            logs_section = (
                "<section class='span'><div class='section-head'><div><h2>Logs &amp; alerts</h2>"
                "<p class='muted'>Recent Osprey records; the host journal remains the full log.</p></div>"
                f"<strong id='log-alert' class='{alert_class}'>{esc(alert_text)}</strong></div>"
                "<div class='controls log-controls'><label>Show <select id='log-level' aria-label='Minimum log level'>"
                "<option value='INFO'>All records</option><option value='WARNING'>Warnings and errors</option>"
                "<option value='ERROR'>Errors only</option></select></label>"
                "<button type='button' id='refresh-logs'>Refresh</button></div>"
                "<pre id='log-output' class='log-output' aria-live='polite'>Loading recent logs…</pre></section>"
            )
            # Framing is a single operator task: put the browser preview and
            # PTZ buttons in the first two grid cells, side by side on desktop.
        frigate = self.frigate_settings()
        frigate_url = esc(frigate["base_url"] or "")
        frigate_section = (
            "<section><h2>Frigate connection</h2>"
            "<p class='muted'>Osprey connects to an existing Frigate instance; it never manages its configuration.</p>"
            f"<form id='frigate-form'><label>Base URL <input id='frigate-url' type='url' required "
            f"value='{frigate_url}' placeholder='http://frigate:5000'></label> "
            "<button class='primary' type='submit'>Save connection</button></form>"
            "<p id='frigate-feedback' class='muted' aria-live='polite'>"
            f"{'Configured' if frigate['configured'] else 'Not configured'}</p></section>"
        )
        body = metrics + live_section + controls + camera_section + settings + logs_section + frigate_section + bandwidth + presets + endpoints

        state = "adopted" if camera else "waiting"
        badge = "ok" if camera else "warn"
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Osprey · operator console</title>"
            "<style>"
            ":root{--bg:#0b1118;--panel:#121c26;--panel-hi:#172634;--fg:#e9f0f4;--muted:#8ea3b2;--line:#263845;"
            "--head:#b4c6d0;--accent:#53d3c1;--accent2:#ff9c68;--ok:#69d391;--warn:#f5c46b;--danger:#f27c86}"
            "*{box-sizing:border-box}"
            "body{margin:0;background:radial-gradient(circle at 85% -20%,#193443 0,transparent 40%),var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
            ".wrap{max-width:1240px;margin:0 auto;padding:28px 22px 40px}"
            "header{display:flex;align-items:center;justify-content:space-between;gap:.6rem;margin-bottom:.2rem}"
            "h1{margin:0;font-size:1.7rem;letter-spacing:-.04em}"
            "h2{margin:0 0 .6rem;font-size:.72rem;text-transform:uppercase;"
            "letter-spacing:.07em;color:var(--head)}"
            ".meta{color:var(--muted);font-size:.85rem;margin:.1rem 0 1.2rem}"
            ".badge{font-size:.68rem;padding:.15rem .55rem;border-radius:999px;"
            "text-transform:uppercase;color:#fff;background:var(--warn)}"
            ".badge.ok{background:var(--ok)}"
            ".grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}"
            "section{grid-column:span 6;background:linear-gradient(145deg,var(--panel-hi),var(--panel));border:1px solid var(--line);border-radius:14px;padding:18px;min-width:0;box-shadow:0 12px 30px #02070b33}"
            "section.span{grid-column:1/-1}"
            ".scroll{overflow-x:auto}"
            "table{border-collapse:collapse;width:100%;font-size:.85rem}"
            "table.kv th{text-align:left;color:var(--muted);font-weight:400;"
            "padding:.22rem 1rem .22rem 0;white-space:nowrap;vertical-align:top}"
            "table.kv td{padding:.22rem 0;word-break:break-word}"
            "table.wide th{text-align:left;color:var(--head);font-weight:600;font-size:.72rem;"
            "text-transform:uppercase;border-bottom:1px solid var(--line);padding:.3rem .8rem .4rem 0;"
            "white-space:nowrap}"
            "table.wide td{padding:.4rem .8rem;border-bottom:1px solid var(--line);white-space:nowrap}"
            "table.wide tr:last-child td{border-bottom:none}"
            ".num{text-align:right;font-variant-numeric:tabular-nums}"
            ".muted{color:var(--muted)}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem}.section-head p{margin:.2rem 0 0}.live-dot{color:var(--accent);font:600 .7rem ui-monospace,monospace;white-space:nowrap}.metric-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.metric{padding:13px;border:1px solid var(--line);border-radius:10px;background:#0b151d}.metric span,.metric small{display:block;color:var(--muted);font-size:.72rem}.metric strong{display:block;margin:.2rem 0;font:600 1.25rem ui-monospace,monospace;color:var(--accent)}"
            ".mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
            "font-size:.82rem;color:var(--accent);word-break:break-all}"
            ".spark-cell{color:var(--accent);line-height:0;min-width:120px}"
            ".spark{display:block;width:100%;max-width:220px}"
            ".dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;"
            "margin-right:.45rem;background:var(--warn);vertical-align:middle}"
            ".dot.ok{background:var(--ok)}"
            "img{max-width:100%;border-radius:6px;display:block}"
            "button,select,input{font:inherit;padding:.48rem .7rem;border:1px solid var(--line);border-radius:7px;background:#0b151d;color:var(--fg)}"
            "button{cursor:pointer;transition:.15s ease}button:hover{border-color:var(--accent);color:var(--accent);transform:translateY(-1px)}button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.controls{display:flex;flex-wrap:wrap;gap:.55rem;align-items:center}.controls label{display:flex;align-items:center;gap:.45rem}.controls input[type=range]{accent-color:var(--accent);padding:0}.primary{background:var(--accent);border-color:var(--accent);color:#071217;font-weight:700}.danger{border-color:var(--danger);color:var(--danger)}"
            ".log-controls{margin:.6rem 0}.log-output{max-height:18rem;overflow:auto;margin:0;padding:.8rem;background:#081018;border:1px solid var(--line);border-radius:8px;color:var(--head);font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word}.alert-ok{color:var(--ok)}.alert-warning{color:var(--warn)}.alert-danger{color:var(--danger)}"
            "@media (prefers-color-scheme: dark){:root{color-scheme:dark}}"
            "@media(max-width:760px){section{grid-column:1/-1}.metric-grid{grid-template-columns:repeat(2,1fr)}.wrap{padding:20px 12px 32px}}@media(max-width:420px){.metric-grid{grid-template-columns:1fr 1fr}.metric strong{font-size:1rem}}"
            "footer{color:var(--muted);opacity:.7;font-size:.72rem;margin-top:1.2rem}"
            "</style></head><body><div class='wrap'>"
            f"<header><div><h1>Osprey <span class='muted'>operator console</span></h1><p class='meta'>Built on Cuckoo · originally published by rjmotion and contributors</p></div><span class='badge {badge}'>{state}</span></header>"
            f"<p class='meta'>{esc(self.host)}:{self.port} · uptime {uptime} · "
            f"{self.subscriptions.count} event subscriber(s) · window {int(BANDWIDTH_WINDOW)}s</p>"
            f"<div class='grid'>{body}</div>"
            "<footer>Snapshot fallback refreshes every 5s · operator actions are sent to the local controller</footer>"
            "<script>async function post(path,data){const e=document.getElementById('feedback');try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const j=await r.json();if(e)e.textContent=r.ok?(j.message||'Command completed'):(j.error||'Command failed');return{r:r,j:j}}catch(err){if(e)e.textContent='Controller unavailable — check the camera connection';return null}}const step=document.getElementById('step-size'),stepOut=document.getElementById('step-value');if(step)step.oninput=()=>stepOut.textContent=step.value+'%';document.querySelectorAll('[data-axis]').forEach(b=>b.onclick=()=>post('/control/step',{axis:b.dataset.axis,direction:Number(b.dataset.direction),step:Number(step?.value||4)}));const zoom=document.getElementById('zoom-level'),zoomOut=document.getElementById('zoom-value');if(zoom){let timer;zoom.oninput=()=>{zoomOut.textContent=zoom.value+'%';clearTimeout(timer);timer=setTimeout(()=>post('/control/zoom',{percent:Number(zoom.value)}),350)}}const home=document.getElementById('save-home');if(home)home.onclick=async()=>{home.disabled=true;home.textContent='Saving…';await post('/control/home',{});home.disabled=false;home.textContent='Save current view as Home'};const recover=document.getElementById('recover-ptz');if(recover)recover.onclick=async()=>{recover.disabled=true;await post('/control/ptz/recover',{});setTimeout(()=>{recover.disabled=false},5000)};const preset=document.getElementById('save-preset');if(preset)preset.onclick=()=>{const n=document.getElementById('preset-name');if(n.value.trim())post('/control/preset',{action:'save',name:n.value.trim()})};document.querySelectorAll('[data-goto-preset]').forEach(b=>b.onclick=()=>post('/control/preset',{action:'goto',name:b.dataset.gotoPreset}));document.querySelectorAll('[data-track]').forEach(s=>s.onchange=()=>post('/control/codec',{track:s.dataset.track,codec:s.value}));const sf=document.getElementById('snapshot-fallback');if(sf)setInterval(()=>{sf.src='/snapshot/?t='+Date.now()},5000);</script>"
            "<script>const frigateForm=document.getElementById('frigate-form');if(frigateForm)frigateForm.onsubmit=async(e)=>{e.preventDefault();const out=document.getElementById('frigate-feedback');out.textContent='Saving…';const result=await post('/api/frigate',{base_url:document.getElementById('frigate-url').value});if(result)out.textContent=result.r.ok?'Frigate connection saved':'Save failed: '+(result.j.error||'invalid URL')};</script>"
            "<script>async function refreshLogs(){const out=document.getElementById('log-output');if(!out)return;const level=(document.getElementById('log-level')||{}).value||'INFO';try{const response=await fetch('/api/logs?limit=100&level='+encodeURIComponent(level));const data=await response.json();if(!response.ok)throw new Error(data.error||'request failed');out.textContent=data.entries.map(e=>`[${e.timestamp}] ${e.level} ${e.logger}: ${e.message}`).join('\\n')||'No records yet';const alert=document.getElementById('log-alert');if(alert){alert.textContent=`${data.errors} error(s), ${data.warnings} warning(s) in recent log`;alert.className=data.errors?'alert-danger':(data.warnings?'alert-warning':'alert-ok')}}catch(error){out.textContent='Unable to load logs: '+error}}document.getElementById('refresh-logs')?.addEventListener('click',refreshLogs);document.getElementById('log-level')?.addEventListener('change',refreshLogs);refreshLogs();setInterval(refreshLogs,5000);</script>"
            "</div></body></html>"
        )

    # ---------------------------------------------------------------- dispatch

    def handle(self, call: Call, caller: Caller | None = None) -> str:
        """Run one allow-listed operation for ``caller`` (default: trusted local)."""
        operation = operation_for(call)
        if operation is None:
            log.info("unhandled ONVIF action %s", call.action)
            return fault(
                f"Action {call.action} is not implemented", RECEIVER,
                ("ter:ActionNotSupported",),
            )
        handler = getattr(self, operation.handler)
        previous = getattr(self._caller, "value", None)
        self._caller.value = caller or Caller()
        try:
            result = handler(call)
        finally:
            self._caller.value = previous
        assert isinstance(result, str)
        return result

    def dispatch(self, payload: bytes, peer: str = "", subscription: str = "") -> tuple[HTTPStatus, str]:
        """Authenticate, authorize and run one SOAP request.

        Pre-auth operations run without credentials. Every other operation
        needs a valid UsernameToken whose principal holds the operation's role,
        unless ONVIF authentication is not configured at all.
        """
        call = parse_call(payload)
        if call is None:
            return HTTPStatus.BAD_REQUEST, fault(
                "unparseable request", SENDER, ("ter:WellFormed",)
            )
        operation = operation_for(call)
        if operation is None:
            log.info("unhandled ONVIF action %s", call.action)
            return HTTPStatus.INTERNAL_SERVER_ERROR, fault(
                f"Action {call.action} is not implemented", RECEIVER,
                ("ter:ActionNotSupported",),
            )
        result = self.onvif_auth.authenticate(payload, peer)
        if operation.role is not PRE_AUTH:
            if not result.ok:
                log.info("ONVIF %s from %s rejected: %s", call.action, peer or "?", result.error)
                return HTTPStatus.UNAUTHORIZED, not_authorized()
            if result.principal is not None and not result.principal.has(operation.role):
                log.info(
                    "ONVIF %s denied for user %s (needs role %s)",
                    call.action, result.principal.username, operation.role,
                )
                return HTTPStatus.BAD_REQUEST, not_authorized(
                    f"user lacks the {operation.role} role for {call.action}"
                )
        caller = Caller(
            principal=result.principal, anonymous=result.anonymous, peer=peer,
            subscription=subscription,
        )
        body = self.handle(call, caller)
        return fault_status(body), body

    # ------------------------------------------------------------------ device

    def _get_system_date_and_time(self, call: Call) -> str:
        now = time.gmtime()
        return envelope(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType>"
            "<tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
            "<tt:UTCDateTime>"
            f"<tt:Time><tt:Hour>{now.tm_hour}</tt:Hour><tt:Minute>{now.tm_min}</tt:Minute>"
            f"<tt:Second>{now.tm_sec}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{now.tm_year}</tt:Year><tt:Month>{now.tm_mon}</tt:Month>"
            f"<tt:Day>{now.tm_mday}</tt:Day></tt:Date>"
            "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )

    def _set_system_date_and_time(self, call: Call) -> str:
        # Osprey follows the host clock (keep it on NTP). Accept and ignore so a
        # client's "sync time" step does not abort its setup flow.
        log.info("ignoring SetSystemDateAndTime; Osprey uses the host clock")
        return envelope("<tds:SetSystemDateAndTimeResponse/>")

    def _get_device_information(self, call: Call) -> str:
        camera = self.backend.camera()
        ident = self.device_identity()
        return envelope(
            "<tds:GetDeviceInformationResponse>"
            f"<tds:Manufacturer>{html.escape(ident.manufacturer)}</tds:Manufacturer>"
            f"<tds:Model>{html.escape(ident.model)}</tds:Model>"
            f"<tds:FirmwareVersion>{html.escape(camera.firmware if camera and camera.firmware else '0')}</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{html.escape(ident.serial)}</tds:SerialNumber>"
            f"<tds:HardwareId>{html.escape(ident.hardware_id)}</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )

    def _device_capabilities_xml(self) -> str:
        return (
            f"<tt:Device><tt:XAddr>{self.address(DEVICE_PATH)}</tt:XAddr>"
            "<tt:Network><tt:IPFilter>false</tt:IPFilter>"
            "<tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
            "<tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>"
            # SystemCapabilities is a strict sequence and every element below
            # is required by the schema, SupportedVersions at least once.
            "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve>"
            "<tt:DiscoveryBye>true</tt:DiscoveryBye>"
            "<tt:RemoteDiscovery>false</tt:RemoteDiscovery>"
            "<tt:SystemBackup>false</tt:SystemBackup>"
            "<tt:SystemLogging>false</tt:SystemLogging>"
            "<tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
            "<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tt:SupportedVersions>"
            "</tt:System></tt:Device>"
        )

    def _get_capabilities(self, call: Call) -> str:
        categories = {
            (element.text or "").strip()
            for element in call.body.iter() if local_name(element.tag) == "Category"
        } - {""}
        wanted = categories or {"All"}

        def want(name: str) -> bool:
            return "All" in wanted or name in wanted

        ptz = (
            f"<tt:PTZ><tt:XAddr>{self.address(PTZ_PATH)}</tt:XAddr></tt:PTZ>"
            if self.ptz_enabled and want("PTZ")
            else ""
        )
        parts = [
            # ONVIF Capabilities is a strict sequence: Analytics, Device, Events,
            # Imaging, Media, PTZ. A real client (zeep, which Home Assistant uses)
            # validates against the schema and silently drops any element out of
            # order — so a mis-ordered Events block means HA never creates motion
            # sensors.
            self._device_capabilities_xml() if want("Device") else "",
            (
                f"<tt:Events><tt:XAddr>{self.address(EVENTS_PATH)}</tt:XAddr>"
                "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
                "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
                "<tt:WSPausableSubscriptionManagerInterfaceSupport>false"
                "</tt:WSPausableSubscriptionManagerInterfaceSupport></tt:Events>"
            ) if want("Events") else "",
            f"<tt:Imaging><tt:XAddr>{self.address(IMAGING_PATH)}</tt:XAddr></tt:Imaging>"
            if want("Imaging") else "",
            (
                f"<tt:Media><tt:XAddr>{self.address(MEDIA_PATH)}</tt:XAddr>"
                "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>"
                "<tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
                "</tt:StreamingCapabilities></tt:Media>"
            ) if want("Media") else "",
            ptz,
        ]
        body = "".join(parts)
        if not body:
            return fault("no capability in the requested category", RECEIVER, (
                "ter:ActionNotSupported", "ter:NoSuchService",
            ))
        return envelope(
            f"<tds:GetCapabilitiesResponse><tds:Capabilities>{body}"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )

    def _service_capabilities_inner(self, namespace: str) -> str:
        """The ``Capabilities`` element of one service, in that service's namespace."""
        if namespace == TDS:
            return (
                "<tds:Capabilities>"
                '<tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false" '
                'DynDNS="false" HostnameFromDHCP="false" NTP="0"/>'
                '<tds:Security UsernameToken="true" HttpDigest="true" TLS1.0="false" '
                'TLS1.1="false" TLS1.2="false"/>'
                '<tds:System DiscoveryResolve="false" DiscoveryBye="true" RemoteDiscovery="false" '
                'SystemBackup="false" SystemLogging="false" FirmwareUpgrade="false" '
                'HttpSnapshot="true"/>'
                "</tds:Capabilities>"
            )
        if namespace == TRT:
            camera = self.backend.camera()
            count = len(camera.tracks) if camera is not None else 0
            return (
                '<trt:Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false">'
                f'<trt:ProfileCapabilities MaximumNumberOfProfiles="{max(1, count)}"/>'
                '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" '
                'RTP_RTSP_TCP="true" NonAggregateControl="false" NoRTSPStreaming="false"/>'
                "</trt:Capabilities>"
            )
        if namespace == TPTZ:
            value = "true" if self.ptz_enabled else "false"
            return (
                '<tptz:Capabilities EFlip="false" Reverse="false" '
                f'GetCompatibleConfigurations="false" MoveStatus="{value}" '
                f'StatusPosition="{value}"/>'
            )
        if namespace == TIMG:
            return '<timg:Capabilities ImageStabilization="false"/>'
        return (
            '<tev:Capabilities WSSubscriptionPolicySupport="false" WSPullPointSupport="true" '
            'WSPausableSubscriptionManagerInterfaceSupport="false" MaxNotificationProducers="0" '
            f'MaxPullPoints="{Subscriptions.MAX_TOTAL}"/>'
        )

    def _get_services(self, call: Call) -> str:
        include = call.text("IncludeCapability").lower() == "true"
        entries = [
            (TDS, DEVICE_PATH),
            (TRT, MEDIA_PATH),
            (TIMG, IMAGING_PATH),
            (TEV, EVENTS_PATH),
        ]
        if self.ptz_enabled:
            entries.insert(2, (TPTZ, PTZ_PATH))
        body = "".join(
            f"<tds:Service><tds:Namespace>{namespace}</tds:Namespace>"
            f"<tds:XAddr>{self.address(path)}</tds:XAddr>"
            + (f"<tds:Capabilities>{self._service_capabilities_inner(namespace)}</tds:Capabilities>" if include else "")
            + "<tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version>"
            "</tds:Service>"
            for namespace, path in entries
        )
        return envelope(f"<tds:GetServicesResponse>{body}</tds:GetServicesResponse>")

    def _get_wsdl_url(self, call: Call) -> str:
        return envelope(
            "<tds:GetWsdlUrlResponse><tds:WsdlUrl>http://www.onvif.org/onvif/ver10/device/wsdl/devicemgmt.wsdl"
            "</tds:WsdlUrl></tds:GetWsdlUrlResponse>"
        )

    def _get_endpoint_reference(self, call: Call) -> str:
        return envelope(
            "<tds:GetEndpointReferenceResponse>"
            f"<tds:GUID>{discovery.endpoint_for(self.host, self.port)}</tds:GUID>"
            "</tds:GetEndpointReferenceResponse>"
        )

    def _get_scopes(self, call: Call) -> str:
        configurable = ("onvif://www.onvif.org/name/", "onvif://www.onvif.org/location/")
        body = "".join(
            "<tds:Scopes><tt:ScopeDef>"
            f"{'Configurable' if scope.startswith(configurable) else 'Fixed'}</tt:ScopeDef>"
            f"<tt:ScopeItem>{html.escape(scope)}</tt:ScopeItem></tds:Scopes>"
            for scope in self.scopes()
        )
        return envelope(f"<tds:GetScopesResponse>{body}</tds:GetScopesResponse>")

    def _get_hostname(self, call: Call) -> str:
        return envelope(
            "<tds:GetHostnameResponse><tds:HostnameInformation>"
            "<tt:FromDHCP>false</tt:FromDHCP>"
            f"<tt:Name>{html.escape(self.device_identity().hostname)}</tt:Name>"
            "</tds:HostnameInformation></tds:GetHostnameResponse>"
        )

    def _get_dns(self, call: Call) -> str:
        return envelope(
            "<tds:GetDNSResponse><tds:DNSInformation><tt:FromDHCP>false</tt:FromDHCP>"
            "</tds:DNSInformation></tds:GetDNSResponse>"
        )

    def _get_ntp(self, call: Call) -> str:
        return envelope(
            "<tds:GetNTPResponse><tds:NTPInformation><tt:FromDHCP>false</tt:FromDHCP>"
            "</tds:NTPInformation></tds:GetNTPResponse>"
        )

    def _ipv4_host(self) -> str | None:
        try:
            return str(ipaddress.IPv4Address(self.host))
        except ValueError:
            return None

    def _get_network_interfaces(self, call: Call) -> str:
        ident = self.device_identity()
        address = self._ipv4_host()
        manual = (
            f"<tt:Manual><tt:Address>{address}</tt:Address>"
            f"<tt:PrefixLength>{ident.prefix_length}</tt:PrefixLength></tt:Manual>"
            if address else ""
        )
        return envelope(
            "<tds:GetNetworkInterfacesResponse>"
            '<tds:NetworkInterfaces token="eth0"><tt:Enabled>true</tt:Enabled>'
            f"<tt:Info><tt:Name>eth0</tt:Name><tt:HwAddress>{ident.mac}</tt:HwAddress>"
            "<tt:MTU>1500</tt:MTU></tt:Info>"
            f"<tt:IPv4><tt:Enabled>{'true' if address else 'false'}</tt:Enabled>"
            f"<tt:Config>{manual}<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4>"
            "</tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>"
        )

    def _rtsp_port(self) -> int | None:
        try:
            return urlsplit(self.backend.stream_uri("video1")).port
        except ValueError:
            return None

    def _get_network_protocols(self, call: Call) -> str:
        rtsp = self._rtsp_port()
        body = (
            "<tds:NetworkProtocols><tt:Name>HTTP</tt:Name><tt:Enabled>true</tt:Enabled>"
            f"<tt:Port>{self.port}</tt:Port></tds:NetworkProtocols>"
        )
        if rtsp is not None:
            body += (
                "<tds:NetworkProtocols><tt:Name>RTSP</tt:Name><tt:Enabled>true</tt:Enabled>"
                f"<tt:Port>{rtsp}</tt:Port></tds:NetworkProtocols>"
            )
        return envelope(f"<tds:GetNetworkProtocolsResponse>{body}</tds:GetNetworkProtocolsResponse>")

    def _get_network_default_gateway(self, call: Call) -> str:
        return envelope(
            "<tds:GetNetworkDefaultGatewayResponse><tds:NetworkGateway/>"
            "</tds:GetNetworkDefaultGatewayResponse>"
        )

    def _get_discovery_mode(self, call: Call) -> str:
        return envelope(
            "<tds:GetDiscoveryModeResponse><tds:DiscoveryMode>Discoverable</tds:DiscoveryMode>"
            "</tds:GetDiscoveryModeResponse>"
        )

    def _get_service_capabilities(self, call: Call) -> str:
        return envelope(
            f"<tds:GetServiceCapabilitiesResponse>{self._service_capabilities_inner(TDS)}"
            "</tds:GetServiceCapabilitiesResponse>"
        )

    def _get_media_service_capabilities(self, call: Call) -> str:
        return envelope(
            f"<trt:GetServiceCapabilitiesResponse>{self._service_capabilities_inner(TRT)}"
            "</trt:GetServiceCapabilitiesResponse>"
        )

    def _get_imaging_service_capabilities(self, call: Call) -> str:
        return envelope(
            f"<timg:GetServiceCapabilitiesResponse>{self._service_capabilities_inner(TIMG)}"
            "</timg:GetServiceCapabilitiesResponse>"
        )

    def _get_events_service_capabilities(self, call: Call) -> str:
        return envelope(
            f"<tev:GetServiceCapabilitiesResponse>{self._service_capabilities_inner(TEV)}"
            "</tev:GetServiceCapabilitiesResponse>"
        )

    def _get_ptz_service_capabilities(self, call: Call) -> str:
        return envelope(
            f"<tptz:GetServiceCapabilitiesResponse>{self._service_capabilities_inner(TPTZ)}"
            "</tptz:GetServiceCapabilitiesResponse>"
        )

    # ------------------------------------------------------------------- media

    @staticmethod
    def _encoding(track: VideoTrack) -> str:
        # Media1's VideoEncoding enumeration is JPEG/MPEG4/H264 only. H.265
        # tracks are still reported as "H265" because Home Assistant and Frigate
        # tolerate it; strict Profile S clients (and Protect) need an H.264 track.
        return "H265" if track.codec.value == "h265" else "JPEG" if track.codec.value == "mjpg" else track.codec.value.upper()

    def _source_track(self, camera: Camera | None) -> VideoTrack | None:
        """The largest video track stands in for the camera's one sensor."""
        if camera is None:
            return None
        videos = [track for track in camera.tracks if track.is_video] or list(camera.tracks)
        if not videos:
            return None
        return max(videos, key=lambda track: track.width * track.height)

    def _source_configuration_xml(self, camera: Camera | None, element: str) -> str:
        source = self._source_track(camera)
        width = source.width if source else 1920
        height = source.height if source else 1080
        uses = len(camera.tracks) if camera is not None else 1
        return (
            f'<{element} token="{VIDEO_SOURCE}">'
            f"<tt:Name>{VIDEO_SOURCE}</tt:Name><tt:UseCount>{max(1, uses)}</tt:UseCount>"
            f"<tt:SourceToken>{VIDEO_SOURCE}</tt:SourceToken>"
            f'<tt:Bounds x="0" y="0" width="{width}" height="{height}"/>'
            f"</{element}>"
        )

    def _encoder_xml(self, track: VideoTrack, element: str) -> str:
        """A schema-ordered tt:VideoEncoderConfiguration.

        Sequence: Name, UseCount, Encoding, Resolution, Quality, RateControl,
        [MPEG4], [H264], Multicast, SessionTimeout. UniFi Protect is reported
        to abandon adoption when RateControl is missing; Multicast and
        SessionTimeout are required by the schema.
        """
        token = html.escape(track.name, quote=True)
        encoding = self._encoding(track)
        gov = max(1, track.fps) * 2
        h264 = (
            f"<tt:H264><tt:GovLength>{gov}</tt:GovLength>"
            "<tt:H264Profile>Main</tt:H264Profile></tt:H264>"
            if encoding == "H264" else ""
        )
        return (
            f'<{element} token="{token}"><tt:Name>{html.escape(track.name)}</tt:Name>'
            "<tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>{encoding}</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{track.width}</tt:Width>"
            f"<tt:Height>{track.height}</tt:Height></tt:Resolution>"
            "<tt:Quality>5</tt:Quality>"
            f"<tt:RateControl><tt:FrameRateLimit>{track.fps}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{max(1, track.bitrate // 1000)}</tt:BitrateLimit></tt:RateControl>"
            f"{h264}"
            "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>"
            "<tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
            "<tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
            "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
            f"</{element}>"
        )

    def _profile_xml(self, camera: Camera, name: str, prefix: str = "trt:Profiles") -> str:
        track = camera.track(name)
        if track is None:
            return ""
        ptz = ""
        if self.ptz_enabled:
            ptz = (
                f'<tt:PTZConfiguration token="{PTZ_CONFIG}">'
                f"<tt:Name>{PTZ_CONFIG}</tt:Name><tt:UseCount>1</tt:UseCount>"
                f"<tt:NodeToken>{PTZ_NODE}</tt:NodeToken>"
                f"{PTZ_DEFAULT_SPACES}"
                "</tt:PTZConfiguration>"
            )
        return (
            f'<{prefix} token="{html.escape(name, quote=True)}" fixed="true"><tt:Name>{html.escape(name)}</tt:Name>'
            f"{self._source_configuration_xml(camera, 'tt:VideoSourceConfiguration')}"
            f"{self._encoder_xml(track, 'tt:VideoEncoderConfiguration')}"
            f"{ptz}</{prefix}>"
        )

    def _get_profiles(self, call: Call) -> str:
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        body = "".join(self._profile_xml(camera, track.name) for track in camera.tracks)
        return envelope(f"<trt:GetProfilesResponse>{body}</trt:GetProfilesResponse>")

    def _get_profile(self, call: Call) -> str:
        camera = self.backend.camera()
        token = call.text("ProfileToken")
        if camera is None or camera.track(token) is None:
            return invalid_arg("no such profile", "ter:NoProfile")
        return envelope(
            f"<trt:GetProfileResponse>{self._profile_xml(camera, token, 'trt:Profile')}"
            "</trt:GetProfileResponse>"
        )

    def _get_video_sources(self, call: Call) -> str:
        camera = self.backend.camera()
        track = self._source_track(camera)
        width = track.width if track else 1920
        height = track.height if track else 1080
        fps = track.fps if track else 15
        return envelope(
            f'<trt:GetVideoSourcesResponse><trt:VideoSources token="{VIDEO_SOURCE}">'
            f"<tt:Framerate>{float(fps)}</tt:Framerate>"
            f"<tt:Resolution><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height>"
            "</tt:Resolution></trt:VideoSources></trt:GetVideoSourcesResponse>"
        )

    def _get_video_source_configurations(self, call: Call) -> str:
        camera = self.backend.camera()
        return envelope(
            "<trt:GetVideoSourceConfigurationsResponse>"
            f"{self._source_configuration_xml(camera, 'trt:Configurations')}"
            "</trt:GetVideoSourceConfigurationsResponse>"
        )

    def _get_compatible_video_source_configurations(self, call: Call) -> str:
        camera = self.backend.camera()
        token = call.text("ProfileToken")
        if camera is None or camera.track(token) is None:
            return invalid_arg("no such profile", "ter:NoProfile")
        return envelope(
            "<trt:GetCompatibleVideoSourceConfigurationsResponse>"
            f"{self._source_configuration_xml(camera, 'trt:Configurations')}"
            "</trt:GetCompatibleVideoSourceConfigurationsResponse>"
        )

    def _get_video_source_configuration(self, call: Call) -> str:
        camera = self.backend.camera()
        if call.text("ConfigurationToken") != VIDEO_SOURCE:
            return invalid_arg("no such video source configuration", "ter:NoConfig")
        return envelope(
            "<trt:GetVideoSourceConfigurationResponse>"
            f"{self._source_configuration_xml(camera, 'trt:Configuration')}"
            "</trt:GetVideoSourceConfigurationResponse>"
        )

    def _get_video_encoder_configurations(self, call: Call) -> str:
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        body = "".join(self._encoder_xml(track, "trt:Configurations") for track in camera.tracks)
        return envelope(
            f"<trt:GetVideoEncoderConfigurationsResponse>{body}"
            "</trt:GetVideoEncoderConfigurationsResponse>"
        )

    def _get_video_encoder_configuration(self, call: Call) -> str:
        camera = self.backend.camera()
        track = camera.track(call.text("ConfigurationToken")) if camera is not None else None
        if track is None:
            return invalid_arg("no such video encoder configuration", "ter:NoConfig")
        return envelope(
            "<trt:GetVideoEncoderConfigurationResponse>"
            f"{self._encoder_xml(track, 'trt:Configuration')}"
            "</trt:GetVideoEncoderConfigurationResponse>"
        )

    def _get_compatible_video_encoder_configurations(self, call: Call) -> str:
        camera = self.backend.camera()
        track = camera.track(call.text("ProfileToken")) if camera is not None else None
        if track is None:
            return invalid_arg("no such profile", "ter:NoProfile")
        return envelope(
            "<trt:GetCompatibleVideoEncoderConfigurationsResponse>"
            f"{self._encoder_xml(track, 'trt:Configurations')}"
            "</trt:GetCompatibleVideoEncoderConfigurationsResponse>"
        )

    def _get_video_encoder_configuration_options(self, call: Call) -> str:
        """Options for one encoder: each channel has exactly its armed settings."""
        camera = self.backend.camera()
        token = call.text("ConfigurationToken") or call.text("ProfileToken")
        tracks = list(camera.tracks) if camera is not None else []
        if token:
            selected = camera.track(token) if camera is not None else None
            if selected is None:
                return invalid_arg("no such configuration or profile", "ter:NoConfig")
            tracks = [selected]
        h264 = [track for track in tracks if self._encoding(track) == "H264"]
        h264_xml = ""
        if h264:
            resolutions = "".join(
                f"<tt:ResolutionsAvailable><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height>"
                "</tt:ResolutionsAvailable>"
                for width, height in sorted({(track.width, track.height) for track in h264}, reverse=True)
            )
            fps = max(track.fps for track in h264)
            h264_xml = (
                f"<tt:H264>{resolutions}"
                f"<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>{max(1, fps) * 4}</tt:Max></tt:GovLengthRange>"
                f"<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>{max(1, fps)}</tt:Max></tt:FrameRateRange>"
                "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>"
                "<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported></tt:H264>"
            )
        return envelope(
            "<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
            "<tt:QualityRange><tt:Min>0</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>"
            f"{h264_xml}</trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>"
        )

    def _get_guaranteed_number_of_video_encoder_instances(self, call: Call) -> str:
        camera = self.backend.camera()
        tracks = list(camera.tracks) if camera is not None else []
        h264 = sum(1 for track in tracks if self._encoding(track) == "H264")
        return envelope(
            "<trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse>"
            f"<trt:TotalNumber>{len(tracks)}</trt:TotalNumber>"
            f"<trt:H264>{h264}</trt:H264>"
            "</trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse>"
        )

    # Audio is not offered over ONVIF yet: empty lists are valid Profile S
    # answers and keep clients from treating the device as broken.
    def _get_audio_sources(self, call: Call) -> str:
        return envelope("<trt:GetAudioSourcesResponse/>")

    def _get_audio_source_configurations(self, call: Call) -> str:
        return envelope("<trt:GetAudioSourceConfigurationsResponse/>")

    def _get_audio_encoder_configurations(self, call: Call) -> str:
        return envelope("<trt:GetAudioEncoderConfigurationsResponse/>")

    def _get_audio_outputs(self, call: Call) -> str:
        return envelope("<trt:GetAudioOutputsResponse/>")

    def _set_synchronization_point(self, call: Call) -> str:
        # Keyframes repeat on the camera's own GOP and parameter sets are sent
        # ahead of every keyframe, so there is nothing to force here.
        return envelope("<trt:SetSynchronizationPointResponse/>")

    def _set_video_encoder_configuration(self, call: Call) -> str:
        """Re-arm a channel's codec on the fly (ONVIF SetVideoEncoderConfiguration).

        Home Assistant never calls this — it consumes the profiles it is given — but
        a fuller ONVIF client can flip a channel between H.264 and H.265 at runtime,
        and it rides the same adoption-time settings path (a fresh ChangeVideoSettings
        to the camera). The token is the channel (video1/…); Encoding is H264/H265.
        """
        camera = self.backend.camera()
        token = call.attribute("Configuration", "token") or call.text("ConfigurationToken")
        encoding = call.text("Encoding")
        if camera is None or not token or camera.track(token) is None:
            return invalid_arg("no such video encoder configuration", "ter:NoConfig")
        codec = ENCODING_TO_CODEC.get(encoding.upper())
        if codec is None:
            return invalid_arg(f"unsupported encoding {encoding!r}", "ter:ConfigModify")
        if not self.backend.set_encoder(token, codec):
            return fault("could not apply video encoder configuration")
        return envelope("<trt:SetVideoEncoderConfigurationResponse/>")

    def _profile_token(self, call: Call) -> tuple[str, str | None]:
        """The requested profile, and a fault if a known camera lacks it."""
        token = call.text("ProfileToken") or "video1"
        camera = self.backend.camera()
        if camera is not None and camera.tracks and camera.track(token) is None:
            return token, invalid_arg("no such profile", "ter:NoProfile")
        return token, None

    def _get_stream_uri(self, call: Call) -> str:
        token, problem = self._profile_token(call)
        if problem is not None:
            return problem
        uri = self.backend.stream_uri(token)
        return envelope(
            "<trt:GetStreamUriResponse><trt:MediaUri>"
            f"<tt:Uri>{html.escape(uri)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri></trt:GetStreamUriResponse>"
        )

    def snapshot_token(self, username: str, profile: str) -> str:
        mac = hmac.new(self._snapshot_key, f"{username}\0{profile}".encode(), hashlib.sha256).digest()
        return f"{username}.{base64.urlsafe_b64encode(mac[:18]).decode()}"

    def _snapshot_token_principal(self, value: str, profile: str) -> Principal | None:
        username, _, _ = value.partition(".")
        principal = self.onvif_auth.principal(username)
        if principal is None:
            return None
        return principal if hmac.compare_digest(value, self.snapshot_token(username, profile)) else None

    def _get_snapshot_uri(self, call: Call) -> str:
        token, problem = self._profile_token(call)
        if problem is not None:
            return problem
        uri = self.backend.snapshot_uri(token)
        principal = self.caller.principal
        tokenised = principal is not None
        if tokenised:
            assert principal is not None
            separator = "&" if "?" in uri else "?"
            uri = f"{uri}{separator}{SNAPSHOT_TOKEN_PARAM}={self.snapshot_token(principal.username, token)}"
        return envelope(
            "<trt:GetSnapshotUriResponse><trt:MediaUri>"
            f"<tt:Uri>{html.escape(uri)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            f"<tt:InvalidAfterReboot>{'true' if tokenised else 'false'}</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri></trt:GetSnapshotUriResponse>"
        )

    def snapshot_response(
        self, profile: str, authorization: str = "", query_token: str = "",
        peer: str = "", uri: str = "",
    ) -> tuple[HTTPStatus, bytes, dict[str, str]]:
        """Serve ``GET /snapshot/<profile>`` with auth, token check and rate limit.

        With ONVIF auth enabled, the request needs either the capability token
        from GetSnapshotUri or HTTP Digest credentials of a principal holding
        the ``media`` role. Anonymous mode keeps the historical public endpoint.
        """
        camera = self.backend.camera()
        if camera is not None and camera.tracks and camera.track(profile) is None:
            return HTTPStatus.NOT_FOUND, b"unknown profile", {"Content-Type": "text/plain"}
        if self.onvif_auth.configured:
            principal = None
            if query_token:
                principal = self._snapshot_token_principal(query_token, profile)
            if principal is None and authorization:
                principal = self._snapshot_digest.authenticate(authorization, "GET", uri)
            if (
                principal is None
                or not self.onvif_auth.enabled
                or not principal.has(ROLE_MEDIA)
                or (peer and not principal.permits_peer(peer))
            ):
                return HTTPStatus.UNAUTHORIZED, b"authentication required", {
                    "Content-Type": "text/plain",
                    "WWW-Authenticate": self._snapshot_digest.challenge(),
                }
        image = self.snapshots.get()
        if image is None:
            return HTTPStatus.SERVICE_UNAVAILABLE, b"", {"Content-Type": "text/plain"}
        return HTTPStatus.OK, image, {"Content-Type": "image/jpeg", "Cache-Control": "no-store"}

    # --------------------------------------------------------------------- PTZ

    def _get_nodes(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        return envelope(f"<tptz:GetNodesResponse>{self._node_xml('tptz:PTZNode')}</tptz:GetNodesResponse>")

    def _get_node(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        return envelope(f"<tptz:GetNodeResponse>{self._node_xml('tptz:PTZNode')}</tptz:GetNodeResponse>")

    def _node_xml(self, element: str) -> str:
        camera = self.backend.camera()
        presets = len(camera.presets) if camera else 0
        spaces = _ptz_spaces(camera)
        return (
            f'<{element} token="{PTZ_NODE}"><tt:Name>{PTZ_NODE}</tt:Name>'
            f"<tt:SupportedPTZSpaces>{spaces}</tt:SupportedPTZSpaces>"
            f"<tt:MaximumNumberOfPresets>{max(64, presets)}</tt:MaximumNumberOfPresets>"
            f"<tt:HomeSupported>{'true' if self.ptz_enabled else 'false'}</tt:HomeSupported>"
            f"</{element}>"
        )

    def _get_configurations(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        return envelope(
            "<tptz:GetConfigurationsResponse>"
            f'<tptz:PTZConfiguration token="{PTZ_CONFIG}">'
            f"<tt:Name>{PTZ_CONFIG}</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:NodeToken>{PTZ_NODE}</tt:NodeToken>"
            f"{PTZ_DEFAULT_SPACES}"
            "</tptz:PTZConfiguration></tptz:GetConfigurationsResponse>"
        )

    def _get_configuration(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        return self._get_configurations(call)

    def _get_configuration_options(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        # Other clients (ODM, some NVRs) read move-mode support from here; Home
        # Assistant instead reads it from the PTZConfiguration's Default*Space
        # elements in GetProfiles (see PTZ_DEFAULT_SPACES). Answered for both.
        camera = self.backend.camera()
        spaces = _ptz_spaces(camera)
        return envelope(
            "<tptz:GetConfigurationOptionsResponse><tptz:PTZConfigurationOptions>"
            f"<tt:Spaces>{spaces}</tt:Spaces>"
            "<tt:PTZTimeout><tt:Min>PT1S</tt:Min><tt:Max>PT60S</tt:Max></tt:PTZTimeout>"
            "</tptz:PTZConfigurationOptions></tptz:GetConfigurationOptionsResponse>"
        )

    def _get_status(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        self.backend.refresh_position()
        position, pan_tilt_state, zoom_state, motion_error = camera.motion.snapshot()
        pan = camera.pan_range.to_normalised(position.pan)
        # Mirror the inverted tilt axis from _target_from so ONVIF +Y = up here too.
        tilt = -camera.tilt_range.to_normalised(position.tilt)
        zoom = (camera.zoom_range.to_normalised(position.zoom) + 1.0) / 2.0
        error = f"<tt:Error>{html.escape(motion_error)}</tt:Error>" if motion_error else ""
        return envelope(
            "<tptz:GetStatusResponse><tptz:PTZStatus>"
            f'<tt:Position><tt:PanTilt x="{pan:.4f}" y="{tilt:.4f}"/>'
            f'<tt:Zoom x="{zoom:.4f}"/></tt:Position>'
            f"<tt:MoveStatus><tt:PanTilt>{pan_tilt_state}</tt:PanTilt>"
            f"<tt:Zoom>{zoom_state}</tt:Zoom></tt:MoveStatus>"
            f"{error}"
            f"<tt:UtcTime>{utc()}</tt:UtcTime>"
            "</tptz:PTZStatus></tptz:GetStatusResponse>"
        )

    def _target_from(self, call: Call, camera: Camera, relative: bool) -> Position:
        current = camera.motion.position
        pan_tilt = call.vector("PanTilt")
        zoom = call.vector("Zoom")
        if pan_tilt is not None:
            # ONVIF's tilt axis is +Y = up; this camera's tilt value grows as the
            # head drops, so a raw mapping sends "up" down. Invert Y once here so
            # every client (HA, ODM, …) gets the intuitive direction, and mirror it
            # in GetStatus below so reported position stays consistent.
            pan_tilt = (pan_tilt[0], -pan_tilt[1])
        pan, tilt = current.pan, current.tilt
        if pan_tilt is not None:
            if relative:
                span_pan = camera.pan_range.maximum - camera.pan_range.minimum
                span_tilt = camera.tilt_range.maximum - camera.tilt_range.minimum
                space = call.attribute("PanTilt", "space")
                if space == FOV_TRANSLATION_SPACE:
                    geometry = camera.field_of_view
                    if geometry is None:
                        raise ValueError("FOV-relative movement is not calibrated for this camera")
                    zoom_factor = (
                        camera.zoom_range.to_normalised(current.zoom) + 1.0
                    ) / 2.0
                    horizontal, vertical = geometry.at_zoom(zoom_factor)
                    x = max(-1.0, min(1.0, pan_tilt[0]))
                    y = max(-1.0, min(1.0, pan_tilt[1]))
                    pan_delta = x * horizontal / 2.0 * span_pan / geometry.pan_degrees
                    tilt_delta = y * vertical / 2.0 * span_tilt / geometry.tilt_degrees
                    pan = camera.pan_range.clamp(round(pan + pan_delta))
                    tilt = camera.tilt_range.clamp(round(tilt + tilt_delta))
                else:
                    pan = camera.pan_range.clamp(round(pan + pan_tilt[0] * span_pan / 2))
                    tilt = camera.tilt_range.clamp(round(tilt + pan_tilt[1] * span_tilt / 2))
            else:
                pan = camera.pan_range.from_normalised(pan_tilt[0])
                tilt = camera.tilt_range.from_normalised(pan_tilt[1])
        zoom_value = current.zoom
        if zoom is not None:
            if relative:
                span = camera.zoom_range.maximum - camera.zoom_range.minimum
                zoom_value = camera.zoom_range.clamp(round(zoom_value + zoom[0] * span))
            else:
                # ONVIF zoom is 0..1 where pan and tilt are -1..1.
                zoom_value = camera.zoom_range.from_normalised(zoom[0] * 2.0 - 1.0)
        return Position(pan=pan, tilt=tilt, zoom=zoom_value, focus=current.focus)

    def _absolute_move(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        moved = self.backend.move_absolute(self._target_from(call, camera, relative=False))
        if not moved:
            return fault(self.backend.move_refusal())
        return envelope("<tptz:AbsoluteMoveResponse/>")

    def _relative_move(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        try:
            target = self._target_from(call, camera, relative=True)
        except ValueError as exc:
            return fault(str(exc))
        moved = self.backend.move_absolute(target)
        if not moved:
            return fault(self.backend.move_refusal())
        return envelope("<tptz:RelativeMoveResponse/>")

    def _continuous_move(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        """The camera has no continuous verb, so velocity becomes one relative step.

        A client holding an arrow key sends these repeatedly, which gives the same
        felt behaviour without pretending to a mode the camera does not have.
        """
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        try:
            target = self._target_from(call, camera, relative=True)
        except ValueError as exc:
            return fault(str(exc))
        moved = self.backend.move_absolute(target)
        if not moved:
            return fault(self.backend.move_refusal())
        return envelope("<tptz:ContinuousMoveResponse/>")

    def _stop(self, call: Call) -> str:
        """ONVIF Stop: halt the selected axes where they are now.

        PanTilt and Zoom default to true. The controller re-targets the head at
        its latest reported pose (the camera has no native stop verb), so the
        head may settle a few motor steps past the moment Stop arrived. Stopping
        an idle head succeeds without sending anything.
        """
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")

        def flag(name: str) -> bool:
            value = call.text(name, "true").lower()
            if value not in ("true", "false", "1", "0"):
                raise ValueError(name)
            return value in ("true", "1")

        try:
            pan_tilt, zoom = flag("PanTilt"), flag("Zoom")
        except ValueError as exc:
            return invalid_arg(f"{exc} must be a boolean")
        if not pan_tilt and not zoom:
            return envelope("<tptz:StopResponse/>")
        if not self.backend.stop(pan_tilt, zoom):
            if camera.motion.status == "IDLE":
                return envelope("<tptz:StopResponse/>")
            return fault(self.backend.move_refusal())
        return envelope("<tptz:StopResponse/>")

    def _get_presets(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        body = ""
        for index, preset in sorted(camera.presets.items()):
            pan = camera.pan_range.to_normalised(preset.position.pan)
            tilt = -camera.tilt_range.to_normalised(preset.position.tilt)  # +Y = up, as elsewhere
            zoom = (camera.zoom_range.to_normalised(preset.position.zoom) + 1.0) / 2.0
            body += (
                f'<tptz:Preset token="{index}"><tt:Name>{html.escape(preset.name)}</tt:Name>'
                f'<tt:PTZPosition><tt:PanTilt x="{pan:.4f}" y="{tilt:.4f}"/>'
                f'<tt:Zoom x="{zoom:.4f}"/></tt:PTZPosition></tptz:Preset>'
            )
        return envelope(f"<tptz:GetPresetsResponse>{body}</tptz:GetPresetsResponse>")

    def _goto_preset(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        token = call.text("PresetToken")
        try:
            index = int(token)
        except ValueError:
            return fault("preset tokens are numeric here")
        if not self.backend.goto_preset(index, 1000):
            return fault(self.backend.move_refusal())
        return envelope("<tptz:GotoPresetResponse/>")

    def _set_preset(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        name = call.text("PresetName") or "preset"
        token = call.text("PresetToken")
        index: int | None
        try:
            index = int(token) if token else None
        except ValueError:
            index = None
        assigned = self.backend.set_preset(name, index)
        if assigned is None:
            return fault("the camera is not accepting presets")
        return envelope(
            f'<tptz:SetPresetResponse><tptz:PresetToken>{assigned}</tptz:PresetToken>'
            "</tptz:SetPresetResponse>"
        )

    def _goto_home_position(self, call: Call) -> str:
        """Home is the preset named `home` (the Frigate return target)."""
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        camera = self.backend.camera()
        home = next(
            (p for p in camera.presets.values() if p.name.strip().casefold() == "home"), None
        ) if camera else None
        if home is None:
            return fault("no home position; call SetHomePosition or save one in the console")
        if not self.backend.goto_preset(home.index, 1000):
            return fault(self.backend.move_refusal())
        return envelope("<tptz:GotoHomePositionResponse/>")

    def _set_home_position(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        status, result = self.control_home()
        if status != HTTPStatus.OK:
            return fault(str(result.get("error", "the camera is not accepting presets")))
        return envelope("<tptz:SetHomePositionResponse/>")

    def _remove_preset(self, call: Call) -> str:
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        try:
            index = int(call.text("PresetToken"))
        except ValueError:
            return fault("preset tokens are numeric here")
        if not self.backend.remove_preset(index):
            return fault("no such preset")
        return envelope("<tptz:RemovePresetResponse/>")

    # ----------------------------------------------------------------- imaging

    def _get_imaging_settings(self, call: Call) -> str:
        return envelope(
            "<timg:GetImagingSettingsResponse><timg:ImagingSettings>"
            # ImagingSettings20 sequence: Brightness, ColorSaturation, Contrast, …, Sharpness.
            "<tt:Brightness>50</tt:Brightness><tt:ColorSaturation>50</tt:ColorSaturation>"
            "<tt:Contrast>50</tt:Contrast><tt:Sharpness>50</tt:Sharpness>"
            "</timg:ImagingSettings></timg:GetImagingSettingsResponse>"
        )

    def _get_options(self, call: Call) -> str:
        limits = "<tt:Min>0</tt:Min><tt:Max>100</tt:Max>"
        return envelope(
            "<timg:GetOptionsResponse><timg:ImagingOptions>"
            f"<tt:Brightness>{limits}</tt:Brightness><tt:ColorSaturation>{limits}</tt:ColorSaturation>"
            f"<tt:Contrast>{limits}</tt:Contrast><tt:Sharpness>{limits}</tt:Sharpness>"
            "</timg:ImagingOptions></timg:GetOptionsResponse>"
        )

    # ------------------------------------------------------------------ events

    def _create_pull_point_subscription(self, call: Call) -> str:
        now = time.time()
        requested = call.text("InitialTerminationTime")
        ttl = parse_termination(requested, now) if requested else None
        if requested and ttl is None:
            return invalid_arg("InitialTerminationTime is not a duration or dateTime")
        identifier = self.subscriptions.create(self.caller.owner, ttl)
        if identifier is None:
            return fault("too many pull-point subscriptions", RECEIVER, ("ter:Action", "ter:MaxPullPoints"))
        address = f"{self.address(EVENTS_PATH)}?sub={identifier}"
        expires = self.subscriptions.expires(identifier) or now
        return envelope(
            "<tev:CreatePullPointSubscriptionResponse>"
            "<tev:SubscriptionReference>"
            f"<wsa:Address>{html.escape(address)}</wsa:Address>"
            "</tev:SubscriptionReference>"
            f"<wsnt:CurrentTime>{utc(now)}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{utc(expires)}</wsnt:TerminationTime>"
            "</tev:CreatePullPointSubscriptionResponse>"
        )

    def _caller_subscription(self) -> tuple[str, str | None]:
        """The subscription this request addresses, or a fault.

        The id comes from the subscription address (``?sub=``). Without one, a
        caller that owns exactly one subscription is assumed to mean it; a
        caller can never reach another owner's subscription.
        """
        caller = self.caller
        identifier = caller.subscription
        if not identifier:
            mine = self.subscriptions.owned_by(caller.owner)
            if len(mine) != 1:
                return "", invalid_arg("subscription address required", "ter:InvalidSubscription")
            identifier = mine[0]
        owner = self.subscriptions.owner_of(identifier)
        if owner is None:
            return "", invalid_arg("unknown or expired subscription", "ter:InvalidSubscription")
        if self.caller.principal is not None and owner != caller.owner:
            return "", not_authorized("subscription belongs to another client")
        return identifier, None

    def pull_messages(self, identifier: str, limit: int = 10) -> str:
        if not self.subscriptions.exists(identifier):
            return invalid_arg("unknown or expired subscription", "ter:InvalidSubscription")
        events = self.subscriptions.pull(identifier, limit)
        body = "".join(event.as_xml() for event in events)
        expires = self.subscriptions.expires(identifier) or time.time()
        return envelope(
            "<tev:PullMessagesResponse>"
            f"<tev:CurrentTime>{utc()}</tev:CurrentTime>"
            f"<tev:TerminationTime>{utc(expires)}</tev:TerminationTime>"
            f"{body}</tev:PullMessagesResponse>"
        )

    def _pull_messages(self, call: Call) -> str:
        identifier, problem = self._caller_subscription()
        if problem is not None:
            return problem
        try:
            limit = int(call.text("MessageLimit", "10"))
        except ValueError:
            return invalid_arg("MessageLimit must be an integer")
        return self.pull_messages(identifier, max(1, min(limit, Subscriptions.DEPTH)))

    def _renew(self, call: Call) -> str:
        identifier, problem = self._caller_subscription()
        if problem is not None:
            return problem
        requested = call.text("TerminationTime")
        now = time.time()
        ttl = parse_termination(requested, now) if requested else None
        if requested and ttl is None:
            return invalid_arg("TerminationTime is not a duration or dateTime")
        expires = self.subscriptions.renew(identifier, ttl)
        if expires is None:
            return invalid_arg("unknown or expired subscription", "ter:InvalidSubscription")
        return envelope(
            "<wsnt:RenewResponse>"
            f"<wsnt:TerminationTime>{utc(expires)}</wsnt:TerminationTime>"
            f"<wsnt:CurrentTime>{utc(now)}</wsnt:CurrentTime>"
            "</wsnt:RenewResponse>"
        )

    def _unsubscribe(self, call: Call) -> str:
        identifier, problem = self._caller_subscription()
        if problem is not None:
            return problem
        self.subscriptions.drop(identifier)
        return envelope("<wsnt:UnsubscribeResponse/>")

    def _set_event_synchronization_point(self, call: Call) -> str:
        return envelope("<tev:SetSynchronizationPointResponse/>")

    def _get_event_properties(self, call: Call) -> str:
        return envelope(
            "<tev:GetEventPropertiesResponse>"
            "<tev:TopicNamespaceLocation>"
            "http://www.onvif.org/onvif/ver10/topics/topicns.xml"
            "</tev:TopicNamespaceLocation>"
            "<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>"
            f"<wstop:TopicSet xmlns:wstop=\"http://docs.oasis-open.org/wsn/t-1\">"
            "<tns1:RuleEngine><CellMotionDetector><Motion wstop:topic=\"true\"/>"
            "</CellMotionDetector><MyRuleDetector>"
            "<PeopleDetect wstop:topic=\"true\"/><VehicleDetect wstop:topic=\"true\"/>"
            "</MyRuleDetector></tns1:RuleEngine></wstop:TopicSet>"
            "<wsnt:TopicExpressionDialect>"
            "http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete"
            "</wsnt:TopicExpressionDialect>"
            "</tev:GetEventPropertiesResponse>"
        )


def _fmt_rate(bytes_per_sec: float) -> str:
    bits = float(bytes_per_sec) * 8.0
    for unit in ("bps", "kbps", "Mbps", "Gbps"):
        if bits < 1000 or unit == "Gbps":
            return f"{bits:.1f} {unit}"
        bits /= 1000.0
    return f"{bits:.1f} Gbps"


def _fmt_bytes(nbytes: float) -> str:
    value = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _sparkline(series: list[float], width: int = 200, height: int = 30) -> str:
    """An inline SVG (area + line) of a byte-rate series, oldest→newest.

    Self-contained, no JS or external assets, and uses currentColor so it inherits
    the theme's accent in both light and dark mode. A flat/empty series is baseline.
    """
    if len(series) < 2:
        return "<span class='muted'>—</span>"
    peak = max(series) or 1.0
    step = width / float(len(series) - 1)
    points = [
        (index * step, height - 2 - (value / peak) * (height - 4))
        for index, value in enumerate(series)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"0,{height} {line} {width},{height}"
    return (
        f"<svg class='spark' viewBox='0 0 {width} {height}' width='{width}' "
        f"height='{height}' preserveAspectRatio='none' role='img'>"
        f"<polygon points='{area}' fill='currentColor' fill-opacity='0.15'/>"
        f"<polyline points='{line}' fill='none' stroke='currentColor' "
        "stroke-width='1.5' stroke-linejoin='round'/></svg>"
    )


def canonical_camera_id(camera: Camera) -> str:
    """Stable URL-safe identity; never use display names or array indexes."""
    mac = "".join(character for character in camera.mac.lower() if character.isalnum())
    if len(mac) != 12:
        raise ValueError("camera MAC is required for a canonical camera id")
    return f"g5-ptz-{mac}"


class CameraRegistry:
    """Thread-safe collection of independently constructed camera services."""

    def __init__(self, services: Services | None = None) -> None:
        self._items: dict[str, Services] = {}
        self._lock = threading.RLock()
        if services is not None:
            self.register(services)

    def register(self, services: Services) -> str:
        camera = services.backend.camera()
        if camera is None or not camera.adopted:
            raise ValueError("cannot register an unadopted camera")
        identifier = canonical_camera_id(camera)
        with self._lock:
            existing = self._items.get(identifier)
            if existing is not None and existing is not services:
                raise ValueError(f"camera id already registered: {identifier}")
            self._items[identifier] = services
        return identifier

    def get(self, identifier: str) -> Services | None:
        with self._lock:
            return self._items.get(identifier)

    def ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._items))

    def remove(self, identifier: str) -> None:
        with self._lock:
            self._items.pop(identifier, None)


# -------------------------------------------------------------------- transport


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def services(self) -> Services:
        server = self.server
        assert isinstance(server, OnvifServer)
        return getattr(self, "_active_services", server.services)

    def _select_camera(self, root: str) -> str | None:
        """Select an explicit camera-scoped route, rejecting ambiguity."""
        if not root.startswith("/cameras/"):
            self._active_services = self.server.services  # type: ignore[attr-defined]
            self._active_camera_id = None
            return root
        parts = root.split("/", 3)
        if len(parts) != 4 or not parts[2] or not parts[3]:
            self._send(HTTPStatus.NOT_FOUND, b"camera route requires id and resource", "text/plain")
            return None
        server = self.server
        assert isinstance(server, OnvifServer)
        selected = server.registry.get(parts[2]) if server.registry is not None else None
        if selected is None:
            self._send(HTTPStatus.NOT_FOUND, b"unknown camera", "text/plain")
            return None
        self._active_services = selected
        self._active_camera_id = parts[2]
        return "/" + parts[3]

    def log_message(self, format: str, *args: object) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by http.server
        root, _, _ = self.path.partition("?")
        selected_root = self._select_camera(root)
        if selected_root is None:
            return
        root = selected_root
        if root == SETUP_PATH:
            if (
                self.services.read_only
                or self.services.setup is None
                or not self.services.setup.pending
            ):
                self._send(HTTPStatus.NOT_FOUND, b"setup is unavailable", "text/plain")
                return
            self._setup_page()
            return
        if root == LOGIN_PATH:
            self._login_page()
            return
        if root == HEALTH_PATH:
            status, health = self.services.health()
            self._send(status, json.dumps(health, separators=(",", ":")).encode(), "application/json")
            return
        if root == FRIGATE_API_PATH:
            if self.services.read_only:
                self._send(HTTPStatus.NOT_FOUND, b"operator console is disabled on this endpoint", "text/plain")
                return
            if self._require_admin():
                self._send(HTTPStatus.OK, json.dumps(self.services.frigate_settings()).encode(), "application/json")
            return
        if root == LOGS_API_PATH:
            if self.services.read_only:
                self._send(HTTPStatus.NOT_FOUND, b"operator console is disabled on this endpoint", "text/plain")
                return
            if not self._require_admin():
                return
            self._logs_json()
            return
        if root in ("/", "/status", "/status/"):
            if self.services.read_only:
                self._send(HTTPStatus.NOT_FOUND, b"operator console is disabled on this endpoint", "text/plain")
                return
            if self.services.setup is not None and self.services.setup.pending:
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", SETUP_PATH)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._require_admin():
                return
            page = self.services.status_page()
            server = self.server
            active_id = getattr(self, "_active_camera_id", None)
            if active_id:
                page = page.replace("/api/frigate", f"/cameras/{html.escape(active_id)}/api/frigate")
                page = page.replace(LOGS_API_PATH, f"/cameras/{html.escape(active_id)}{LOGS_API_PATH}")
            if isinstance(server, OnvifServer) and server.registry is not None and len(server.registry.ids()) > 1:
                links = "".join(
                    f"<a href='/cameras/{html.escape(identifier)}/status'>{html.escape(identifier)}</a> "
                    for identifier in server.registry.ids()
                )
                page = page.replace("<body>", f"<body><nav aria-label='Camera selection'><strong>Camera:</strong> {links}</nav>", 1)
            self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
            return
        if root.startswith(SNAPSHOT_PATH):
            # Snapshot URLs are consumed by Frigate, Protect and other ONVIF
            # clients. With ONVIF auth enabled they need the capability token
            # from GetSnapshotUri or HTTP Digest credentials (media role).
            _, _, query = self.path.partition("?")
            values = parse_qs(query, keep_blank_values=False)
            status, body, headers = self.services.snapshot_response(
                unquote(root[len(SNAPSHOT_PATH):]).strip("/"),
                authorization=self.headers.get("Authorization", ""),
                query_token=values.get(SNAPSHOT_TOKEN_PARAM, [""])[0],
                peer=str(self.client_address[0]),
                uri=self.path,
            )
            content_type = headers.pop("Content-Type", "text/plain")
            self._send(status, body, content_type, headers)
            return
        if root.startswith(PREVIEW_PATH):
            if self.services.read_only:
                self._send(HTTPStatus.NOT_FOUND, b"preview is disabled on this endpoint", "text/plain")
                return
            if not self._require_admin():
                return
            token = unquote(root[len(PREVIEW_PATH):]).strip("/")
            self._serve_preview(token)
            return
        self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")

    def _logs_json(self) -> None:
        """Serve a bounded, redacted log tail to the authenticated console."""
        _, _, query = self.path.partition("?")
        values = parse_qs(query, keep_blank_values=False)
        try:
            limit = int(values.get("limit", ["100"])[0])
        except (TypeError, ValueError):
            self._send(HTTPStatus.BAD_REQUEST, b'{"error":"limit must be an integer"}', "application/json")
            return
        if not 1 <= limit <= 500:
            self._send(HTTPStatus.BAD_REQUEST, b'{"error":"limit must be between 1 and 500"}', "application/json")
            return
        level_name = values.get("level", ["INFO"])[0].upper()
        minimum = logging._nameToLevel.get(level_name)
        if minimum is None or minimum < logging.INFO:
            self._send(HTTPStatus.BAD_REQUEST, b'{"error":"level must be INFO, WARNING, or ERROR"}', "application/json")
            return
        payload = self.services.log_entries(limit, minimum)
        self._send(HTTPStatus.OK, json.dumps(payload, separators=(",", ":")).encode(), "application/json")

    def _serve_preview(self, token: str) -> None:
        camera = self.services.backend.camera()
        if camera is None or not camera.adopted or camera.track(token) is None:
            self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
            return
        server = self.server
        assert isinstance(server, OnvifServer)
        preview_lock = server.preview_lock(getattr(self, "_active_camera_id", None))
        if not preview_lock.acquire(blocking=False):
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"preview busy", "text/plain")
            return
        uri = self.services.backend.stream_uri(token)
        # The RTSP server is Cuckoo's own local process; loopback avoids routing
        # the preview through the LAN and keeps this endpoint independent of the
        # advertised client-facing hostname.
        try:
            parts = urlsplit(uri)
            if parts.scheme == "rtsp" and parts.port is not None:
                uri = parts._replace(netloc=f"127.0.0.1:{parts.port}").geturl()
            process = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                 "-i", uri, "-an", "-vf", "fps=5,scale=640:-2", "-f", "mpjpeg", "-q:v", "6", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError):
            preview_lock.release()
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"preview unavailable", "text/plain")
            return
        server._preview_processes.add(process)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=ffmpeg")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            server._preview_processes.discard(process)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
            preview_lock.release()

    def do_POST(self) -> None:  # noqa: N802 - name fixed by http.server
        root, _, _ = self.path.partition("?")
        selected_root = self._select_camera(root)
        if selected_root is None:
            return
        root = selected_root
        if root == SETUP_API_PATH:
            if (
                self.services.read_only
                or self.services.setup is None
                or not self.services.setup.pending
            ):
                self._send(HTTPStatus.NOT_FOUND, b"setup is unavailable", "text/plain")
                return
            self._setup_submit()
            return
        if root == LOGIN_PATH:
            self._login()
            return
        if root in (CONTROL_STEP_PATH, CONTROL_HOME_PATH, CONTROL_PRESET_PATH, CONTROL_ZOOM_PATH, CONTROL_RECOVER_PATH, "/control/codec", FRIGATE_API_PATH):
            if self.services.read_only:
                self._send(HTTPStatus.FORBIDDEN, b"controls are disabled on this endpoint", "text/plain")
                return
            if not self._require_admin(csrf=True):
                return
            self._control_json(root)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        # SOAP is XML and is parsed synchronously.  Bound the body before
        # reading it so an unauthenticated endpoint cannot be used for memory
        # exhaustion (and reject malformed negative lengths).
        if length < 0 or length > MAX_SOAP_REQUEST_BYTES:
            self._reject_oversized(length)
            return
        payload = self.rfile.read(length) if length else b""
        status, body = self.services.dispatch(
            payload, peer=str(self.client_address[0]),
            subscription=self._subscription_from_path(),
        )
        self._send(status, body.encode(), "application/soap+xml; charset=utf-8")

    def _reject_oversized(self, length: int) -> None:
        """Answer 413 without buffering the body, then close.

        Closing while the client is still sending makes the kernel reset the
        connection, and the client may never read the 413. So a body up to
        ``MAX_DISCARD_BYTES`` is read and thrown away in small chunks (bounded
        time and memory); anything larger is refused without draining.
        """
        self.close_connection = True
        if 0 < length <= MAX_DISCARD_BYTES:
            remaining = length
            try:
                self.connection.settimeout(DISCARD_TIMEOUT_SEC)
                while remaining > 0:
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                return
        try:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"request too large", "text/plain")
        except OSError:
            pass

    def _session(self) -> tuple[str, str] | None:
        cookie = self.headers.get("Cookie", "")
        session = next((part.split("=", 1)[1] for part in cookie.split("; ")
                        if part.startswith(f"{SESSION_COOKIE}=")), "")
        token = self.services.auth.csrf(session)
        return (session, token) if token else None

    def _require_admin(self, *, csrf: bool = False) -> bool:
        auth = self.services.auth
        if not auth.enabled:
            return True
        session = self._session()
        if session is None:
            if not csrf:
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", LOGIN_PATH)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(HTTPStatus.UNAUTHORIZED, b'{"error":"login required"}', "application/json")
            return False
        if csrf:
            supplied = self.headers.get("X-CSRF-Token", "")
            if not supplied:
                supplied = next((part.split("=", 1)[1] for part in self.headers.get("Cookie", "").split("; ")
                                 if part.startswith("osprey_csrf=")), "")
            if not hmac.compare_digest(supplied, session[1]):
                # Drain the rejected request so HTTP/1.1 keep-alive cannot
                # interpret its JSON bytes as the next request line.
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 <= length <= 4096:
                        self.rfile.read(length)
                except ValueError:
                    pass
                self._send(HTTPStatus.FORBIDDEN, b'{"error":"invalid csrf token"}', "application/json")
                return False
        return True

    def _setup_page(self, error: str = "") -> None:
        """Render the unauthenticated, token-gated first-run wizard."""
        setup = self.services.setup
        if setup is None or not setup.pending:
            self._send(HTTPStatus.NOT_FOUND, b"setup is unavailable", "text/plain")
            return
        message = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        body = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Set up Osprey</title><style>body{font:16px system-ui;max-width:48rem;margin:5vh auto;padding:1.5rem;background:#101820;color:#eef}"
                "fieldset{border:1px solid #38505e;border-radius:10px;margin:1rem 0;padding:1rem}label{display:block;margin:.7rem 0}"
                "input,button{font:inherit;padding:.65rem;width:100%;box-sizing:border-box}input{background:#182631;color:#fff;border:1px solid #536b78;border-radius:5px}"
                "button{background:#6ee7b7;border:0;border-radius:5px;font-weight:700;cursor:pointer}.error{color:#fca5a5}.hint{color:#a7bac5;font-size:.88rem}"
                ".credentials{white-space:pre-wrap;background:#0b1118;padding:1rem;border-radius:6px;display:none}</style>"
                "<h1>Set up Osprey</h1><p class='hint'>This one-time wizard creates the controller configuration and deployment secrets."
                " Copy the setup token from the Osprey startup log before submitting.</p>"
                f"{message}<form id='setup-form'><fieldset><legend>Network</legend>"
                "<label>Advertised host <input name='host' required placeholder='osprey.example.lan'></label>"
                "<label>Bind address <input name='bind' value='0.0.0.0' required></label></fieldset>"
                "<fieldset><legend>Camera</legend>"
                "<label>Name <input name='name' required placeholder='Driveway PTZ'></label>"
                "<label>Protect MAC address <input name='mac' required placeholder='AA:BB:CC:DD:EE:FF'></label>"
                "<label>Camera IP address <input name='ip' required placeholder='192.0.2.20'></label></fieldset>"
                "<fieldset><legend>Frigate and access</legend>"
                "<label>Frigate URL <input name='frigate_url' type='url' required placeholder='http://frigate:5000'></label>"
                "<label>Admin password <input name='admin_password' type='password' minlength='8' required autocomplete='new-password'></label>"
                "<label>Setup token <input name='setup_token' type='password' required autocomplete='off'></label></fieldset>"
                "<button type='submit'>Save configuration</button></form><p id='result' class='hint'></p>"
                "<pre id='credentials' class='credentials'></pre><script>"
                "const form=document.getElementById('setup-form'),result=document.getElementById('result'),out=document.getElementById('credentials');"
                "form.onsubmit=async e=>{e.preventDefault();result.textContent='Saving…';const data=Object.fromEntries(new FormData(form));"
                "try{const r=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});"
                "const j=await r.json();if(!r.ok)throw Error(j.error||'setup failed');result.textContent='Saved. Restart Osprey to apply the configuration.';"
                "out.textContent='Save these generated credentials now; they are shown only once.\\n\\n'+Object.entries(j.generated_credentials||{}).map(([k,v])=>k+'='+v).join('\\n');out.style.display='block';form.querySelector('button').disabled=true;"
                "}catch(err){result.textContent=err.message}};</script>")
        self._send(HTTPStatus.OK, body.encode(), "text/html; charset=utf-8")

    def _setup_submit(self) -> None:
        setup = self.services.setup
        if setup is None or not setup.pending:
            self._send(HTTPStatus.NOT_FOUND, b'{"error":"setup is unavailable"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16 * 1024:
                raise SetupError("invalid request size")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise SetupError("Content-Type must be application/json")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise SetupError("setup payload must be an object")
            token = self.headers.get("X-Setup-Token", "") or str(payload.pop("setup_token", ""))
            result = setup.save(payload, token)
        except (SetupError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, json.dumps({"error": str(exc)}).encode(), "application/json")
            return
        self._send(HTTPStatus.OK, json.dumps(result).encode(), "application/json")

    def _login_page(self, error: str = "") -> None:
        if not self.services.auth.enabled:
            self._send(HTTPStatus.NOT_FOUND, b"admin authentication is not configured", "text/plain")
            return
        message = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        body = ("<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Sign in · Osprey</title><style>body{font:16px system-ui;max-width:26rem;margin:12vh auto;padding:1.5rem;background:#101820;color:#eef}"
                "input,button{font:inherit;padding:.7rem;margin:.4rem 0;width:100%;box-sizing:border-box}button{background:#6ee7b7;border:0;font-weight:700}.error{color:#fca5a5}</style>"
                f"<h1>Osprey operator console</h1><p>Sign in to control the camera.</p>{message}"
                "<form method='post' action='/login'><label>Password<input name='password' type='password' autocomplete='current-password' required autofocus></label><button>Sign in</button></form>")
        self._send(HTTPStatus.OK, body.encode(), "text/html; charset=utf-8")

    def _login(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 2048:
                raise ValueError
            values = parse_qs(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, b"invalid login request", "text/plain")
            return
        session = self.services.auth.login(values.get("password", [""])[0])
        if session is None:
            self._login_page("Invalid password")
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}={session[0]}; HttpOnly; SameSite=Lax; Path=/")
        self.send_header("Set-Cookie", f"osprey_csrf={session[1]}; SameSite=Lax; Path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _control_json(self, root: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 4096:
                raise ValueError
            raw = self.rfile.read(length) if length else b""
        except ValueError:
            self._send(HTTPStatus.BAD_REQUEST, json.dumps({"error": "invalid request size"}).encode(), "application/json")
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, json.dumps({"error": "Content-Type must be application/json"}).encode(), "application/json")
            return
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, json.dumps({"error": "invalid JSON request"}).encode(), "application/json")
            return
        if root == CONTROL_STEP_PATH:
            status, result = self.services.control_step(value)
        elif root == CONTROL_HOME_PATH:
            status, result = self.services.control_home()
        elif root == CONTROL_PRESET_PATH:
            status, result = self.services.control_preset(value)
        elif root == CONTROL_ZOOM_PATH:
            status, result = self.services.control_zoom(value)
        elif root == CONTROL_RECOVER_PATH:
            status, result = self.services.control_recover()
        elif root == FRIGATE_API_PATH:
            status, result = self.services.set_frigate_url(value.get("base_url") if isinstance(value, dict) else None)
        else:
            status, result = self.services.control_codec(value)
        self._send(status, json.dumps(result).encode(), "application/json")

    def _subscription_from_path(self) -> str:
        _, _, query = self.path.partition("?")
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key == "sub":
                return value
        return ""

    def _send(
        self, status: HTTPStatus, body: bytes, content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)


class OnvifServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, services: Services, port: int = ONVIF_PORT,
                 auth: AdminAuth | None = None,
                 registry: CameraRegistry | None = None,
                 bind_host: str = "0.0.0.0") -> None:
        self.services = services
        if auth is not None:
            self.services.auth = auth
        self.registry = registry
        self._preview_processes: set[subprocess.Popen[bytes]] = set()
        self._preview_locks: dict[str | None, threading.Lock] = {None: threading.Lock()}
        # Compatibility for integrations/tests that inspect the single-camera lock.
        self._preview_lock = self._preview_locks[None]
        super().__init__((bind_host, port), _Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def preview_lock(self, camera_id: str | None) -> threading.Lock:
        if camera_id is None:
            return self._preview_locks[None]
        return self._preview_locks.setdefault(camera_id, threading.Lock())

    def start(self) -> None:
        log.info("onvif listening on :%d%s", self.port, DEVICE_PATH)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        for process in tuple(self._preview_processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
