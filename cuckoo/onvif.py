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
import json
import logging
import hashlib
import hmac
import os
import secrets
import base64
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

from media import BANDWIDTH_WINDOW
from model import PAN, TILT, ZOOM, Camera, Position
from frigate_config import Store
from logbuffer import BUFFER, LogBuffer
from setup_wizard import SetupError, SetupManager

ONVIF_PORT: Final = 8000
DEVICE_PATH: Final = "/onvif/device_service"
MEDIA_PATH: Final = "/onvif/media_service"
PTZ_PATH: Final = "/onvif/ptz_service"
IMAGING_PATH: Final = "/onvif/imaging_service"
EVENTS_PATH: Final = "/onvif/events_service"
MAX_SOAP_REQUEST_BYTES: Final = 256 * 1024
SNAPSHOT_PATH: Final = "/snapshot/"
PREVIEW_PATH: Final = "/preview/"
CONTROL_STEP_PATH: Final = "/control/step"
CONTROL_HOME_PATH: Final = "/control/home"
CONTROL_PRESET_PATH: Final = "/control/preset"
CONTROL_ZOOM_PATH: Final = "/control/zoom"
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


@dataclass(frozen=True)
class OnvifAuth:
    """Optional WS-Security UsernameToken verifier.

    Disabled when ``username`` is unset, preserving camera/Frigate behaviour.
    PasswordDigest is ``Base64(SHA1(nonce + created + password))`` per ONVIF.
    """
    username: str | None = None
    password: str | None = None
    max_skew_seconds: int = 300
    _replay: dict[bytes, float] = field(default_factory=dict, compare=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    # A client can submit a fresh nonce on every request. Keep replay
    # protection bounded so authenticated-but-untrusted clients cannot grow
    # this in-memory set without limit.
    MAX_REPLAY: ClassVar[int] = 4096

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
        return cls(username or None, password or None)

    @property
    def configured(self) -> bool:
        """Whether an incomplete or complete authentication configuration exists."""
        return self.username is not None or self.password is not None

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)

    def valid(self, payload: bytes) -> bool:
        if not self.enabled:
            # Anonymous mode is only valid when authentication is completely
            # unset. A manually constructed partial config must fail closed.
            return not self.configured
        try:
            root = ElementTree.fromstring(payload)
            token = next((e for e in root.iter() if local_name(e.tag) == "UsernameToken"), None)
            if token is None:
                return False
            values = {local_name(e.tag): (e.text or "") for e in token.iter()}
            nonce = base64.b64decode(values.get("Nonce", ""), validate=True)
            created = values.get("Created", "")
            digest = base64.b64decode(values.get("Password", ""), validate=True)
            if not 16 <= len(nonce) <= 64 or len(digest) != 20 or not created:
                return False
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
                return False
            now = time.time()
            if abs(now - parsed.timestamp()) > self.max_skew_seconds:
                return False
            key = hashlib.sha256(nonce + created.encode() + values.get("Username", "").encode()).digest()
            with self._lock:
                for old, when in list(self._replay.items()):
                    if now - when > self.max_skew_seconds:
                        del self._replay[old]
            if key in self._replay:
                return False
            identity_ok = hmac.compare_digest(values.get("Username", ""), self.username or "")
            expected = hashlib.sha1(nonce + created.encode() + (self.password or "").encode()).digest()
            valid = identity_ok and hmac.compare_digest(digest, expected)
            if valid:
                if len(self._replay) >= self.MAX_REPLAY:
                    oldest = min(self._replay, key=lambda candidate: self._replay[candidate])
                    self._replay.pop(oldest, None)
                self._replay[key] = now
            return valid
        except (ElementTree.ParseError, ValueError, TypeError, OverflowError):
            return False

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
}

MANUFACTURER: Final = "cuckoo"
PTZ_NODE: Final = "PTZNode"
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
    # Why a move cannot be dispatched right now, in the camera's own terms:
    # "PTZ channel unavailable" / "the camera is already moving" / generic.
    move_refusal: Callable[[], str] = lambda: "the camera is not accepting movement"
    # Re-arm a channel's codec live: (profile/config token, codec "h264"/"h265").
    set_encoder: Callable[[str, str], bool] = lambda _t, _c: False
    # Per-track runtime telemetry keyed by track name (see media.Hub.stats): the
    # windowed byte-rate + series, lifetime bytes, frames, subscribers, playable.
    telemetry: Callable[[], dict[str, dict[str, object]]] = dict


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


class Subscriptions:
    """Pull-point subscriptions, each with its own backlog."""

    DEPTH: Final = 100

    def __init__(self) -> None:
        self._queues: dict[str, deque[Event]] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def create(self) -> str:
        with self._lock:
            self._counter += 1
            identifier = f"sub{self._counter}"
            self._queues[identifier] = deque(maxlen=self.DEPTH)
        return identifier

    def drop(self, identifier: str) -> None:
        with self._lock:
            self._queues.pop(identifier, None)

    def publish(self, event: Event) -> None:
        with self._lock:
            for queue in self._queues.values():
                queue.append(event)

    def pull(self, identifier: str, limit: int = 10) -> list[Event]:
        with self._lock:
            queue = self._queues.get(identifier)
            if queue is None:
                return []
            return [queue.popleft() for _ in range(min(limit, len(queue)))]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._queues)


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


def fault(reason: str) -> str:
    return envelope(
        "<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code>"
        f"<s:Reason><s:Text xml:lang=\"en\">{html.escape(reason)}</s:Text></s:Reason></s:Fault>"
    )


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


# ---------------------------------------------------------------------- services


class Services:
    """Turns parsed calls into SOAP responses, using only the device model."""

    def __init__(self, backend: Backend, host: str, port: int = ONVIF_PORT,
                 auth: AdminAuth | None = None, onvif_auth: OnvifAuth | None = None,
        read_only: bool = False, logs: LogBuffer | None = None,
        setup: SetupManager | None = None,
        frigate_url: str | None = None) -> None:
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
        """Whether this ONVIF persona exposes or accepts PTZ operations."""
        camera = self.backend.camera()
        return not self.read_only and camera is not None and camera.is_ptz

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
                ("Motion", "moving" if not camera.motion.settled else "idle"),
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
            metric_cards = (
                f"<div class='metric-grid'>"
                f"<div class='metric'><span>PTZ state</span><strong>{'MOVING' if not camera.motion.settled else 'IDLE'}</strong><small>{esc(camera.motion.status)}</small></div>"
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
                    "<button type='button' id='save-home'>Save current view as home</button></div>"
                    "<p id='feedback' role='status' aria-live='polite'></p>"
                    "<p class='muted'>Steps are 4% of the configured range. Saving creates or replaces Home.</p></section>")
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
            "<script>async function post(path,data){const e=document.getElementById('feedback');try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const j=await r.json();if(e)e.textContent=r.ok?(j.message||'Command completed'):(j.error||'Command failed');return{r:r,j:j}}catch(err){if(e)e.textContent='Controller unavailable — check the camera connection';return null}}const step=document.getElementById('step-size'),stepOut=document.getElementById('step-value');if(step)step.oninput=()=>stepOut.textContent=step.value+'%';document.querySelectorAll('[data-axis]').forEach(b=>b.onclick=()=>post('/control/step',{axis:b.dataset.axis,direction:Number(b.dataset.direction),step:Number(step?.value||4)}));const zoom=document.getElementById('zoom-level'),zoomOut=document.getElementById('zoom-value');if(zoom){let timer;zoom.oninput=()=>{zoomOut.textContent=zoom.value+'%';clearTimeout(timer);timer=setTimeout(()=>post('/control/zoom',{percent:Number(zoom.value)}),350)}}const home=document.getElementById('save-home');if(home)home.onclick=async()=>{home.disabled=true;home.textContent='Saving…';await post('/control/home',{});home.disabled=false;home.textContent='Save current view as Home'};const preset=document.getElementById('save-preset');if(preset)preset.onclick=()=>{const n=document.getElementById('preset-name');if(n.value.trim())post('/control/preset',{action:'save',name:n.value.trim()})};document.querySelectorAll('[data-goto-preset]').forEach(b=>b.onclick=()=>post('/control/preset',{action:'goto',name:b.dataset.gotoPreset}));document.querySelectorAll('[data-track]').forEach(s=>s.onchange=()=>post('/control/codec',{track:s.dataset.track,codec:s.value}));const sf=document.getElementById('snapshot-fallback');if(sf)setInterval(()=>{sf.src='/snapshot/?t='+Date.now()},5000);</script>"
            "<script>const frigateForm=document.getElementById('frigate-form');if(frigateForm)frigateForm.onsubmit=async(e)=>{e.preventDefault();const out=document.getElementById('frigate-feedback');out.textContent='Saving…';const result=await post('/api/frigate',{base_url:document.getElementById('frigate-url').value});if(result)out.textContent=result.r.ok?'Frigate connection saved':'Save failed: '+(result.j.error||'invalid URL')};</script>"
            "<script>async function refreshLogs(){const out=document.getElementById('log-output');if(!out)return;const level=(document.getElementById('log-level')||{}).value||'INFO';try{const response=await fetch('/api/logs?limit=100&level='+encodeURIComponent(level));const data=await response.json();if(!response.ok)throw new Error(data.error||'request failed');out.textContent=data.entries.map(e=>`[${e.timestamp}] ${e.level} ${e.logger}: ${e.message}`).join('\\n')||'No records yet';const alert=document.getElementById('log-alert');if(alert){alert.textContent=`${data.errors} error(s), ${data.warnings} warning(s) in recent log`;alert.className=data.errors?'alert-danger':(data.warnings?'alert-warning':'alert-ok')}}catch(error){out.textContent='Unable to load logs: '+error}}document.getElementById('refresh-logs')?.addEventListener('click',refreshLogs);document.getElementById('log-level')?.addEventListener('change',refreshLogs);refreshLogs();setInterval(refreshLogs,5000);</script>"
            "</div></body></html>"
        )

    # ---------------------------------------------------------------- dispatch

    def handle(self, call: Call) -> str:
        if call.action == "GetServiceCapabilities" and call.namespace == NAMESPACES["tptz"]:
            return self._get_ptz_service_capabilities(call)
        handler = getattr(self, f"_{_snake(call.action)}", None)
        if handler is None:
            log.info("unhandled ONVIF action %s", call.action)
            return fault(f"Action {call.action} is not implemented")
        result = handler(call)
        assert isinstance(result, str)
        return result

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

    def _get_device_information(self, call: Call) -> str:
        camera = self.backend.camera()
        return envelope(
            "<tds:GetDeviceInformationResponse>"
            f"<tds:Manufacturer>{MANUFACTURER}</tds:Manufacturer>"
            f"<tds:Model>{html.escape(camera.model if camera else 'unknown')}</tds:Model>"
            f"<tds:FirmwareVersion>{html.escape(camera.firmware if camera else '0')}</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{html.escape(camera.mac if camera else '000000000000')}</tds:SerialNumber>"
            f"<tds:HardwareId>{html.escape(camera.model if camera else 'unknown')}</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )

    def _get_capabilities(self, call: Call) -> str:
        ptz = (
            f"<tt:PTZ><tt:XAddr>{self.address(PTZ_PATH)}</tt:XAddr></tt:PTZ>"
            if self.ptz_enabled
            else ""
        )
        return envelope(
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            # ONVIF Capabilities is a strict sequence: Device, Events, Imaging,
            # Media, PTZ. A real client (zeep, which Home Assistant uses) validates
            # against the schema and silently drops any element out of order — so a
            # mis-ordered Events block means HA never creates motion sensors.
            f"<tt:Device><tt:XAddr>{self.address(DEVICE_PATH)}</tt:XAddr>"
            "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve>"
            "<tt:DiscoveryBye>true</tt:DiscoveryBye></tt:System></tt:Device>"
            f"<tt:Events><tt:XAddr>{self.address(EVENTS_PATH)}</tt:XAddr>"
            "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
            "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
            "<tt:WSPausableSubscriptionManagerInterfaceSupport>false"
            "</tt:WSPausableSubscriptionManagerInterfaceSupport></tt:Events>"
            f"<tt:Imaging><tt:XAddr>{self.address(IMAGING_PATH)}</tt:XAddr></tt:Imaging>"
            f"<tt:Media><tt:XAddr>{self.address(MEDIA_PATH)}</tt:XAddr>"
            "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            "</tt:StreamingCapabilities></tt:Media>"
            f"{ptz}"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )

    def _get_services(self, call: Call) -> str:
        entries = [
            ("http://www.onvif.org/ver10/device/wsdl", DEVICE_PATH),
            ("http://www.onvif.org/ver10/media/wsdl", MEDIA_PATH),
            ("http://www.onvif.org/ver20/imaging/wsdl", IMAGING_PATH),
            ("http://www.onvif.org/ver10/events/wsdl", EVENTS_PATH),
        ]
        if self.ptz_enabled:
            entries.insert(2, ("http://www.onvif.org/ver20/ptz/wsdl", PTZ_PATH))
        body = "".join(
            f"<tds:Service><tds:Namespace>{namespace}</tds:Namespace>"
            f"<tds:XAddr>{self.address(path)}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version>"
            "</tds:Service>"
            for namespace, path in entries
        )
        return envelope(f"<tds:GetServicesResponse>{body}</tds:GetServicesResponse>")

    def _get_scopes(self, call: Call) -> str:
        camera = self.backend.camera()
        scopes = [
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/Profile/Streaming",
            f"onvif://www.onvif.org/name/{camera.name if camera else 'cuckoo'}",
        ]
        if self.ptz_enabled:
            scopes.append("onvif://www.onvif.org/type/ptz")
        body = "".join(
            "<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef>"
            f"<tt:ScopeItem>{html.escape(scope)}</tt:ScopeItem></tds:Scopes>"
            for scope in scopes
        )
        return envelope(f"<tds:GetScopesResponse>{body}</tds:GetScopesResponse>")

    def _get_service_capabilities(self, call: Call) -> str:
        return envelope(
            "<tds:GetServiceCapabilitiesResponse><tds:Capabilities>"
            '<tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false"/>'
            '<tds:System DiscoveryResolve="false" DiscoveryBye="true"/>'
            "</tds:Capabilities></tds:GetServiceCapabilitiesResponse>"
        )

    def _get_ptz_service_capabilities(self, call: Call) -> str:
        camera = self.backend.camera()
        supported = self.ptz_enabled
        value = "true" if supported else "false"
        return envelope(
            "<tptz:GetServiceCapabilitiesResponse>"
            f'<tptz:Capabilities EFlip="false" Reverse="false" '
            f'GetCompatibleConfigurations="false" MoveStatus="{value}" '
            f'StatusPosition="{value}"/>'
            "</tptz:GetServiceCapabilitiesResponse>"
        )

    # ------------------------------------------------------------------- media

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
        encoding = "H265" if track.codec.value == "h265" else track.codec.value.upper()
        return (
            f'<{prefix} token="{html.escape(name, quote=True)}" fixed="true"><tt:Name>{html.escape(name)}</tt:Name>'
            f'<tt:VideoSourceConfiguration token="VideoSource">'
            "<tt:Name>VideoSource</tt:Name><tt:UseCount>1</tt:UseCount>"
            "<tt:SourceToken>VideoSource</tt:SourceToken>"
            f'<tt:Bounds x="0" y="0" width="{track.width}" height="{track.height}"/>'
            "</tt:VideoSourceConfiguration>"
            f'<tt:VideoEncoderConfiguration token="{html.escape(name, quote=True)}">'
            f"<tt:Name>{html.escape(name)}</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>{encoding}</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{track.width}</tt:Width>"
            f"<tt:Height>{track.height}</tt:Height></tt:Resolution>"
            "<tt:Quality>5</tt:Quality>"
            f"<tt:RateControl><tt:FrameRateLimit>{track.fps}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{track.bitrate // 1000}</tt:BitrateLimit></tt:RateControl>"
            "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
            "</tt:VideoEncoderConfiguration>"
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
            return fault("no such profile")
        return envelope(
            f"<trt:GetProfileResponse>{self._profile_xml(camera, token, 'trt:Profile')}"
            "</trt:GetProfileResponse>"
        )

    def _get_video_sources(self, call: Call) -> str:
        camera = self.backend.camera()
        track = camera.tracks[0] if camera and camera.tracks else None
        width = track.width if track else 1920
        height = track.height if track else 1080
        fps = track.fps if track else 15
        return envelope(
            '<trt:GetVideoSourcesResponse><trt:VideoSources token="VideoSource">'
            f"<tt:Framerate>{fps}</tt:Framerate>"
            f"<tt:Resolution><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height>"
            "</tt:Resolution></trt:VideoSources></trt:GetVideoSourcesResponse>"
        )

    def _get_video_encoder_configurations(self, call: Call) -> str:
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        body = "".join(
            f'<trt:Configurations token="{html.escape(track.name, quote=True)}"><tt:Name>{html.escape(track.name)}</tt:Name>'
            "<tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>{'H265' if track.codec.value == 'h265' else track.codec.value.upper()}"
            "</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{track.width}</tt:Width>"
            f"<tt:Height>{track.height}</tt:Height></tt:Resolution>"
            "</trt:Configurations>"
            for track in camera.tracks
        )
        return envelope(
            f"<trt:GetVideoEncoderConfigurationsResponse>{body}"
            "</trt:GetVideoEncoderConfigurationsResponse>"
        )

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
            return fault("no such video encoder configuration")
        codec = ENCODING_TO_CODEC.get(encoding.upper())
        if codec is None:
            return fault(f"unsupported encoding {encoding!r}")
        if not self.backend.set_encoder(token, codec):
            return fault("could not apply video encoder configuration")
        return envelope("<trt:SetVideoEncoderConfigurationResponse/>")

    def _get_stream_uri(self, call: Call) -> str:
        token = call.text("ProfileToken") or "video1"
        uri = self.backend.stream_uri(token)
        return envelope(
            "<trt:GetStreamUriResponse><trt:MediaUri>"
            f"<tt:Uri>{html.escape(uri)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri></trt:GetStreamUriResponse>"
        )

    def _get_snapshot_uri(self, call: Call) -> str:
        token = call.text("ProfileToken") or "video1"
        return envelope(
            "<trt:GetSnapshotUriResponse><trt:MediaUri>"
            f"<tt:Uri>{html.escape(self.backend.snapshot_uri(token))}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri></trt:GetSnapshotUriResponse>"
        )

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
            "<tt:HomeSupported>false</tt:HomeSupported>"
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
        if not self.ptz_enabled:
            return fault("PTZ is disabled on this ONVIF endpoint")
        # Each step completes on its own, so there is nothing to interrupt.
        self.backend.refresh_position()
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
            "<tt:Brightness>50</tt:Brightness><tt:Contrast>50</tt:Contrast>"
            "<tt:ColorSaturation>50</tt:ColorSaturation><tt:Sharpness>50</tt:Sharpness>"
            "</timg:ImagingSettings></timg:GetImagingSettingsResponse>"
        )

    def _get_options(self, call: Call) -> str:
        limits = "<tt:Min>0</tt:Min><tt:Max>100</tt:Max>"
        return envelope(
            "<timg:GetOptionsResponse><timg:ImagingOptions>"
            f"<tt:Brightness>{limits}</tt:Brightness><tt:Contrast>{limits}</tt:Contrast>"
            f"<tt:ColorSaturation>{limits}</tt:ColorSaturation><tt:Sharpness>{limits}</tt:Sharpness>"
            "</timg:ImagingOptions></timg:GetOptionsResponse>"
        )

    # ------------------------------------------------------------------ events

    def _create_pull_point_subscription(self, call: Call) -> str:
        identifier = self.subscriptions.create()
        address = f"{self.address(EVENTS_PATH)}?sub={identifier}"
        return envelope(
            "<tev:CreatePullPointSubscriptionResponse>"
            "<tev:SubscriptionReference>"
            f"<wsa:Address>{html.escape(address)}</wsa:Address>"
            "</tev:SubscriptionReference>"
            f"<wsnt:CurrentTime>{utc()}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{utc(time.time() + 60)}</wsnt:TerminationTime>"
            "</tev:CreatePullPointSubscriptionResponse>"
        )

    def pull_messages(self, identifier: str, limit: int = 10) -> str:
        events = self.subscriptions.pull(identifier, limit)
        body = "".join(event.as_xml() for event in events)
        return envelope(
            "<tev:PullMessagesResponse>"
            f"<tev:CurrentTime>{utc()}</tev:CurrentTime>"
            f"<tev:TerminationTime>{utc(time.time() + 60)}</tev:TerminationTime>"
            f"{body}</tev:PullMessagesResponse>"
        )

    def _pull_messages(self, call: Call) -> str:
        # Without a subscription id on the URL, serve whichever one exists.
        return self.pull_messages(self._only_subscription())

    def _renew(self, call: Call) -> str:
        return envelope(
            "<wsnt:RenewResponse>"
            f"<wsnt:CurrentTime>{utc()}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{utc(time.time() + 60)}</wsnt:TerminationTime>"
            "</wsnt:RenewResponse>"
        )

    def _unsubscribe(self, call: Call) -> str:
        self.subscriptions.drop(self._only_subscription())
        return envelope("<wsnt:UnsubscribeResponse/>")

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

    def _only_subscription(self) -> str:
        with self.subscriptions._lock:  # noqa: SLF001 - same module
            return next(iter(self.subscriptions._queues), "")


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


def _snake(action: str) -> str:
    out: list[str] = []
    for index, character in enumerate(action):
        if character.isupper() and index:
            out.append("_")
        out.append(character.lower())
    return "".join(out)


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
            if self.services.read_only or self.services.setup is None:
                self._send(HTTPStatus.NOT_FOUND, b"setup is unavailable", "text/plain")
                return
            self._setup_page()
            return
        if root == LOGIN_PATH:
            self._login_page()
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
        if self.path.startswith(SNAPSHOT_PATH):
            # Snapshot URLs are also consumed by Frigate and ONVIF clients.
            # They carry an opaque track token and remain public on the
            # camera-facing ONVIF port; the operator UI and controls are auth'd.
            image = self.services.backend.snapshot()
            if image is None:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"", "text/plain")
                return
            self._send(HTTPStatus.OK, image, "image/jpeg")
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
            if self.services.read_only or self.services.setup is None:
                self._send(HTTPStatus.NOT_FOUND, b"setup is unavailable", "text/plain")
                return
            self._setup_submit()
            return
        if root == LOGIN_PATH:
            self._login()
            return
        if root in (CONTROL_STEP_PATH, CONTROL_HOME_PATH, CONTROL_PRESET_PATH, CONTROL_ZOOM_PATH, "/control/codec", FRIGATE_API_PATH):
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
            length = 0
        # SOAP is XML and is parsed synchronously.  Bound the body before
        # reading it so an unauthenticated endpoint cannot be used for memory
        # exhaustion (and reject malformed negative lengths).
        if length < 0 or length > MAX_SOAP_REQUEST_BYTES:
            self.close_connection = True
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"request too large", "text/plain")
            return
        payload = self.rfile.read(length) if length else b""
        if not self.services.onvif_auth.valid(payload):
            self._send(HTTPStatus.UNAUTHORIZED, fault("ONVIF authentication required").encode(), "text/xml")
            return
        call = parse_call(payload)
        if call is None:
            self._send(HTTPStatus.BAD_REQUEST, fault("unparseable request").encode(), "text/xml")
            return
        subscription = self._subscription_from_path()
        if call.action == "PullMessages" and subscription:
            body = self.services.pull_messages(subscription)
        elif call.action == "Unsubscribe" and subscription:
            self.services.subscriptions.drop(subscription)
            body = envelope("<wsnt:UnsubscribeResponse/>")
        else:
            body = self.services.handle(call)
        status = HTTPStatus.INTERNAL_SERVER_ERROR if "s:Fault" in body else HTTPStatus.OK
        self._send(status, body.encode(), "application/soap+xml; charset=utf-8")

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
        if setup is None:
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
        if setup is None:
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

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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
