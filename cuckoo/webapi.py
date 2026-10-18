"""Browser app shell, versioned JSON API (``/api/v1``) and live event stream.

The ONVIF transport (``onvif._Handler``) offers every request to
:meth:`WebApp.handle` first; anything not owned here falls through to the
SOAP, legacy console and media routes unchanged.  This module therefore never
edits SOAP behaviour and never generates HTML: it serves the static files in
``cuckoo/web/`` and JSON.

Trust boundary: the same operator authentication as the legacy console
(:class:`onvif.AdminAuth`).  Every mutation needs the ``X-CSRF-Token`` header,
every body is JSON of bounded size, and every PTZ value is validated and
clamped to the camera's announced motor range.  See
``specs/002-operator-web-ui/contracts/http-api.md`` for the contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs, quote, unquote

import ptz
import websecurity
from model import Camera, Position

if TYPE_CHECKING:  # pragma: no cover - import cycle only for annotations
    from onvif import OnvifServer, Services, _Handler

log = logging.getLogger("cuckoo.webapi")

API_PREFIX: Final = "/api/v1"
STATIC_PREFIX: Final = "/ui/"
WEB_ROOT: Final = Path(__file__).resolve().parent / "web"
DEFAULT_CAMERA: Final = "default"
MAX_JSON_BYTES: Final = 4096
MAX_SSE_CLIENTS: Final = 16
MAX_THUMBNAILS: Final = 64
MAX_THUMBNAIL_BYTES: Final = 2 * 1024 * 1024
# A continuous-move velocity of 1.0 becomes one step of this fraction of the
# current horizontal field of view (or of the axis span without calibration).
CONTINUOUS_FOV_FRACTION: Final = 0.25
CONTINUOUS_SPAN_FRACTION: Final = 0.03
CONTINUOUS_ZOOM_FRACTION: Final = 0.05

_STATIC_TYPES: Final = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}
_STATIC_NAME: Final = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
_IMPORT_MAP: Final = re.compile(r'<script type="importmap">(.*?)</script>', re.S)
_CAMERA_ID: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PRESET_NAME_MAX: Final = 64


class ApiError(Exception):
    """A request the API refuses, rendered as ``{error:{code,message,hint}}``."""

    def __init__(self, status: HTTPStatus, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint

    def body(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        return {"error": error}


# ----------------------------------------------------------------- SSE framing


def sse_frame(event: str, data: object, event_id: int | None = None) -> bytes:
    """Encode one Server-Sent Event; ``data`` is JSON on a single line."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", event):
        raise ValueError("invalid event name")
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False)
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {int(event_id)}")
    # json.dumps never emits raw newlines, but split defensively so a future
    # payload type cannot inject a second field.
    lines.extend(f"data: {part}" for part in payload.splitlines() or [""])
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def sse_comment(text: str = "") -> bytes:
    return f": {text.replace(chr(10), ' ')}\n\n".encode("utf-8")


# ----------------------------------------------------------- state snapshots


def _normalised(camera: Camera, position: Position) -> dict[str, float]:
    """ONVIF-style coordinates: pan/tilt -1..1 (tilt +1 = up), zoom 0..1."""
    return {
        "pan": round(camera.pan_range.to_normalised(position.pan), 4),
        "tilt": round(-camera.tilt_range.to_normalised(position.tilt), 4),
        "zoom": round((camera.zoom_range.to_normalised(position.zoom) + 1.0) / 2.0, 4),
    }


def ptz_state(camera: Camera | None, *, enabled: bool = True) -> dict[str, object]:
    """PTZ status, the timeout/channel error, and the current position."""
    if camera is None or not camera.is_ptz or not enabled:
        return {"supported": False, "status": "UNAVAILABLE", "error": None}
    position, pan_tilt, zoom, error = camera.motion.snapshot()
    if "UNKNOWN" in (pan_tilt, zoom):
        status = "UNKNOWN"
    elif "MOVING" in (pan_tilt, zoom):
        status = "MOVING"
    else:
        status = "IDLE"
    return {
        "supported": True,
        "status": status,
        "pan_tilt": pan_tilt,
        "zoom_state": zoom,
        "error": error,
        "available": camera.motion.available,
        "position": _normalised(camera, position),
        "raw": {"pan": position.pan, "tilt": position.tilt, "zoom": position.zoom},
        "updated_at": round(camera.motion.updated_at, 3),
        "fov_calibrated": camera.field_of_view is not None,
    }


def _number(value: object, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def stream_stats(services: "Services") -> dict[str, dict[str, object]]:
    camera = services.backend.camera()
    tele = services.backend.telemetry()
    out: dict[str, dict[str, object]] = {}
    names = [track.name for track in camera.tracks] if camera is not None else list(tele)
    for name in names:
        stat = tele.get(name, {})
        series = stat.get("series")
        out[name] = {
            "playable": bool(stat.get("playable")),
            "rate_bps": round(_number(stat.get("rate_bps")), 1),
            "bytes_in": int(_number(stat.get("bytes_in"))),
            "frames": int(_number(stat.get("frames"))),
            "keyframes": int(_number(stat.get("keyframes"))),
            "subscribers": int(_number(stat.get("subscribers"))),
            "series": [round(_number(v), 1) for v in series][-120:] if isinstance(series, list) else [],
        }
    return out


def health_checks(services: "Services") -> list[dict[str, object]]:
    """Plain-language checks; ``status`` is ok, warn, bad or unknown."""
    camera = services.backend.camera()
    checks: list[dict[str, object]] = []

    def check(identifier: str, label: str, status: str, detail: str,
              actions: tuple[str, ...] = ()) -> None:
        checks.append({"id": identifier, "label": label, "status": status,
                       "detail": detail, "actions": list(actions)})

    if camera is None or not camera.adopted:
        check("adoption", "Camera adoption", "warn", "Waiting for the camera to connect and adopt.")
    else:
        check("adoption", "Camera adoption", "ok", f"{camera.name or camera.mac} is adopted.")
    if camera is not None and camera.is_ptz:
        state = ptz_state(camera)
        error = state.get("error")
        if not camera.motion.available:
            check("ptz_channel", "PTZ channel", "bad",
                  "The PTZ channel is not connected; movement is unavailable.", ("recover",))
        elif state["status"] == "UNKNOWN":
            reason = str(error or "state unknown")
            detail = ("The motor did not report completion; presets and Home cannot be "
                      "saved until the state is recovered."
                      if reason == "motor state timeout" else
                      f"PTZ state is unknown: {reason}.")
            check("motor_state", "PTZ motor state", "bad", detail, ("recover", "home"))
        else:
            check("ptz_channel", "PTZ channel", "ok", f"Connected; motor {str(state['status']).lower()}.")
    # Optional watchdog signal: a backend may expose ``ptz_health()`` returning
    # ``{"status": ..., "detail": ...}``; it is shown verbatim when present.
    extra = getattr(services.backend, "ptz_health", None)
    if callable(extra):
        try:
            value = extra()
        except Exception:  # pragma: no cover - a diagnostic must never break the API
            value = None
        if isinstance(value, dict):
            if "status" in value:
                check("ptz_watchdog", "PTZ watchdog", str(value.get("status", "unknown")),
                      str(value.get("detail", "")), tuple(str(a) for a in value.get("actions", ()) or ()))
            elif "healthy" in value and (value.get("ptz") is not False or value.get("error") == "no camera"):
                rec = value.get("recovery") if isinstance(value.get("recovery"), dict) else {}
                recycles = int(value.get("watchdog_recycles", 0) or 0)
                unanswered = int(value.get("unanswered_position_requests", 0) or 0)
                age = value.get("last_motor_reply_age_sec")
                if value.get("healthy"):
                    w_status = "warn" if unanswered > 0 else "ok"
                    parts = [f"last motor reply {age} s ago" if age is not None else "channel active"]
                    if unanswered:
                        parts.append(f"{unanswered} unanswered poll(s)")
                    if recycles:
                        parts.append(f"{recycles} recycle(s)")
                    w_detail = f"Healthy ({', '.join(parts)})."
                    w_actions: tuple[str, ...] = ("recover",) if unanswered > 0 else ()
                else:
                    w_status = "warn" if rec.get("active") and not rec.get("exhausted") else "bad"
                    if rec.get("active"):
                        w_detail = (
                            f"Recovering PTZ channel "
                            f"(attempt {rec.get('attempts', 0)}/{rec.get('max_attempts', 0)})."
                        )
                    elif value.get("error"):
                        w_detail = f"PTZ unhealthy: {value.get('error')}."
                    else:
                        w_detail = (
                            f"PTZ channel {value.get('channel', 'down')}; "
                            f"state {value.get('state', 'UNKNOWN')}."
                        )
                    w_actions = ("recover",)
                check("ptz_watchdog", "PTZ watchdog", w_status, w_detail, w_actions)
    streams = stream_stats(services)
    for name, stat in streams.items():
        if stat["playable"]:
            check(f"track:{name}", f"Stream {name}", "ok", "Playable.")
        else:
            check(f"track:{name}", f"Stream {name}", "warn",
                  "No decodable keyframe yet; Frigate cannot use this track.")
    frigate = services.frigate_settings()
    check("frigate", "Frigate connection",
          "ok" if frigate["configured"] else "warn",
          "Base URL configured." if frigate["configured"] else "No Frigate base URL configured.")
    check("onvif_auth", "ONVIF authentication",
          "ok" if services.onvif_auth.enabled else "warn",
          "WS-Security required." if services.onvif_auth.enabled
          else "Anonymous ONVIF is enabled; restrict the port to Frigate and trusted clients.")
    return checks


def preset_list(camera: Camera | None, thumbs: Callable[[int], bool]) -> list[dict[str, object]]:
    if camera is None:
        return []
    return [
        {
            "token": index,
            "name": preset.name,
            "home": preset.name.strip().casefold() == "home",
            "position": _normalised(camera, preset.position),
            "thumbnail": thumbs(index),
        }
        for index, preset in sorted(camera.presets.items())
    ]


# ------------------------------------------------------------------ the app


@dataclass
class _Request:
    handler: "_Handler"
    method: str
    path: str
    query: dict[str, list[str]]


class WebApp:
    """Owns ``/``, ``/ui/*``, ``/api/v1/*`` and ``/cameras/<id>/`` redirects."""

    def __init__(self, server: "OnvifServer", web_root: Path = WEB_ROOT) -> None:
        self.server = server
        self.web_root = web_root
        self.poll_interval = 0.1       # PTZ coalescing period (10 Hz)
        self.streams_interval = 2.0
        self.heartbeat_interval = 15.0
        self.max_sse_clients = MAX_SSE_CLIENTS
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._sse_clients = 0
        self._thumbs: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self._static: dict[str, tuple[float, bytes, str]] = {}
        # Hook for the PTZ watchdog: (services) -> (accepted, message).  When
        # unset, ``Backend.recover_ptz`` is used if the backend defines it,
        # else a position poll is requested (a fresh motor reply clears the
        # timeout state).
        self.recover_hook: Callable[["Services"], tuple[bool, str]] | None = None

    def close(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------- routing

    def handle(self, handler: "_Handler", method: str) -> bool:
        # Handler objects live for a keep-alive connection; reset per request.
        handler._web_body_read = False  # type: ignore[attr-defined]
        raw_path, _, raw_query = handler.path.partition("?")
        path = raw_path
        request = _Request(handler, method, path, parse_qs(raw_query, keep_blank_values=False))
        services = self.server.services
        if path in ("/", "/index.html"):
            if method != "GET":
                return False
            if services.read_only:
                return False
            if services.setup is not None and services.setup.pending:
                handler._redirect("/setup")
                return True
            self._serve_index(handler)
            return True
        if path.startswith(STATIC_PREFIX):
            if method != "GET":
                self._send_json(handler, HTTPStatus.METHOD_NOT_ALLOWED,
                                ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method", "GET only").body())
                return True
            if services.read_only:
                handler._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
                return True
            self._serve_static(handler, path[len(STATIC_PREFIX):])
            return True
        match = re.fullmatch(r"/cameras/([^/]+)/?", path)
        if match and method == "GET":
            if services.read_only:
                return False
            handler._redirect("/#/live?cam=" + quote(unquote(match.group(1)), safe=""))
            return True
        if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
            if services.read_only:
                self._drain(handler)
                self._send_error(handler, ApiError(HTTPStatus.NOT_FOUND, "disabled",
                                                   "the operator API is disabled on this endpoint"))
                return True
            try:
                self._api(request, path[len(API_PREFIX):] or "/")
            except ApiError as exc:
                self._send_error(handler, exc)
            return True
        return False

    def _api(self, request: _Request, path: str) -> None:
        method = request.method
        h = request.handler
        if path == "/session":
            if method == "GET":
                self._send_json(h, HTTPStatus.OK, self._session_body(h))
            elif method == "POST":
                self._login(h)
            elif method == "DELETE":
                self._logout(h)
            else:
                raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method", "unsupported method")
            return
        mutating = method in ("POST", "PUT", "PATCH", "DELETE")
        self._authorize(h, csrf=mutating)
        if path == "/events" and method == "GET":
            self._events(request)
            return
        if path == "/cameras" and method == "GET":
            self._send_json(h, HTTPStatus.OK, {"cameras": [self._camera_summary(i, s) for i, s in self._cameras()]})
            return
        if path == "/logs" and method == "GET":
            self._send_json(h, HTTPStatus.OK, self._logs(request.query))
            return
        match = re.fullmatch(r"/cameras/([^/]+)(/.*)?", path)
        if not match:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "no such API resource")
        camera_id = unquote(match.group(1))
        services = self._resolve(camera_id)
        self._camera_route(h, method, camera_id, services, match.group(2) or "/")

    def _camera_route(self, h: "_Handler", method: str, camera_id: str,
                      services: "Services", rest: str) -> None:
        def body() -> object:
            return self._read_json(h)

        status: HTTPStatus
        result: dict[str, object]
        if rest in ("/", "/state") and method == "GET":
            status, result = HTTPStatus.OK, self.state(camera_id, services)
        elif rest == "/health" and method == "GET":
            status, result = HTTPStatus.OK, {"checks": health_checks(services)}
        elif rest == "/snapshot.jpg" and method == "GET":
            image = services.backend.snapshot()
            if image is None:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "no_snapshot", "no snapshot is available yet")
            h._send(HTTPStatus.OK, image, "image/jpeg", headers=(("Cache-Control", "no-store"),))
            return
        elif rest == "/ptz/move" and method == "POST":
            status, result = self.ptz_move(services, body())
        elif rest == "/ptz/center" and method == "POST":
            status, result = self.ptz_center(services, body())
        elif rest == "/ptz/stop" and method == "POST":
            self._drain(h)
            status, result = self.ptz_stop(services)
        elif rest == "/ptz/home" and method == "POST":
            self._drain(h)
            status, result = self.ptz_home(services)
        elif rest == "/ptz/home/save" and method == "POST":
            self._drain(h)
            status, legacy = services.control_home()
            result = self._from_legacy(status, legacy)
            if status == HTTPStatus.OK:
                self._capture_thumbnail(services, int(str(legacy["token"])))
                self._emit_presets()
        elif rest == "/ptz/recover" and method == "POST":
            self._drain(h)
            status, result = self.ptz_recover(services)
        elif rest == "/presets" and method == "GET":
            status, result = HTTPStatus.OK, {"presets": self._presets(services)}
        elif rest == "/presets" and method == "POST":
            status, result = self.preset_save(services, body())
        elif rest == "/frigate" and method == "GET":
            status, result = HTTPStatus.OK, services.frigate_settings()
        elif rest == "/frigate" and method == "PUT":
            data = body()
            status, legacy = services.set_frigate_url(data.get("base_url") if isinstance(data, dict) else None)
            result = self._from_legacy(status, legacy)
        else:
            preset = re.fullmatch(r"/presets/(\d{1,6})(/goto|/thumbnail\.jpg)?", rest)
            track = re.fullmatch(r"/tracks/([A-Za-z0-9_-]{1,32})", rest)
            if preset:
                token = int(preset.group(1))
                action = preset.group(2) or ""
                if action == "/goto" and method == "POST":
                    self._drain(h)
                    status, result = self.preset_goto(services, token)
                elif action == "/thumbnail.jpg" and method == "GET":
                    image = self._thumb(services, token)
                    if image is None:
                        raise ApiError(HTTPStatus.NOT_FOUND, "no_thumbnail", "no thumbnail for this preset")
                    h._send(HTTPStatus.OK, image, "image/jpeg", headers=(("Cache-Control", "private, max-age=60"),))
                    return
                elif action == "" and method == "PUT":
                    self._drain(h)
                    status, result = self.preset_update(services, token)
                elif action == "" and method == "DELETE":
                    self._drain(h)
                    status, result = self.preset_delete(services, token)
                else:
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method", "unsupported method")
            elif track and method == "PUT":
                data = body()
                codec = data.get("codec") if isinstance(data, dict) and set(data) == {"codec"} else None
                status, legacy = services.control_codec({"track": track.group(1), "codec": codec})
                result = self._from_legacy(status, legacy)
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "no such camera resource")
        self._send_json(h, status, result)

    # ----------------------------------------------------------- cameras

    def _cameras(self) -> list[tuple[str, "Services"]]:
        registry = self.server.registry
        if registry is not None:
            items = [(i, registry.get(i)) for i in registry.ids()]
            found = [(i, s) for i, s in items if s is not None]
            if found:
                return found
        return [(DEFAULT_CAMERA, self.server.services)]

    def _resolve(self, camera_id: str) -> "Services":
        if camera_id == DEFAULT_CAMERA:
            return self.server.services
        registry = self.server.registry
        found = registry.get(camera_id) if registry is not None and _CAMERA_ID.fullmatch(camera_id) else None
        if found is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "unknown_camera", "unknown camera")
        return found

    @staticmethod
    def media_prefix(camera_id: str) -> str:
        """Relative (ingress-safe) prefix for the camera's media routes."""
        return "" if camera_id == DEFAULT_CAMERA else f"cameras/{quote(camera_id, safe='')}/"

    def _camera_summary(self, camera_id: str, services: "Services") -> dict[str, object]:
        camera = services.backend.camera()
        return {
            "id": camera_id,
            "name": (camera.name if camera is not None else "") or camera_id,
            "model": camera.model if camera is not None else "",
            "adopted": bool(camera is not None and camera.adopted),
            "ptz": services.ptz_enabled,
            "status": ptz_state(camera, enabled=services.ptz_enabled)["status"],
        }

    def state(self, camera_id: str, services: "Services") -> dict[str, object]:
        """The full snapshot sent as the first SSE ``state`` event."""
        camera = services.backend.camera()
        media = self.media_prefix(camera_id)
        streams = stream_stats(services)
        preview = next((name for name, stat in streams.items() if stat["playable"]), None)
        tracks: list[dict[str, object]] = []
        rtsp: dict[str, str] = {}
        if camera is not None:
            for track in camera.tracks:
                tracks.append({
                    "name": track.name, "width": track.width, "height": track.height,
                    "fps": track.fps, "codec": track.codec.value, "bitrate": track.bitrate,
                })
                rtsp[track.name] = services.backend.stream_uri(track.name)
        auth = services.auth
        return {
            "camera": None if camera is None else {
                **self._camera_summary(camera_id, services),
                "mac": camera.mac,
                "firmware": camera.firmware,
                "tracks": tracks,
            },
            "ptz": ptz_state(camera, enabled=services.ptz_enabled),
            "streams": streams,
            "presets": self._presets(services),
            "health": {"checks": health_checks(services)},
            "frigate": services.frigate_settings(),
            "media": {
                "preview": f"{media}preview/{quote(preview, safe='')}" if preview else None,
                "snapshot": f"api/v1/cameras/{quote(camera_id, safe='')}/snapshot.jpg",
                "legacy": f"{media}legacy",
            },
            "endpoints": {
                "onvif": services.address("/onvif/device_service"),
                "snapshot": services.address("/snapshot/"),
                "rtsp": rtsp,
            },
            "server": {
                "uptime": int(time.time() - services.started_at),
                "auth": auth.mode,
                "time": round(time.time(), 3),
            },
        }

    # --------------------------------------------------------------- PTZ

    @staticmethod
    def _ptz_camera(services: "Services") -> Camera:
        camera = services.backend.camera()
        if camera is None or not services.ptz_enabled:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "no_ptz", "no PTZ camera is available")
        return camera

    @staticmethod
    def _fields(data: object, allowed: set[str], required: set[str] = set()) -> dict[str, object]:
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", "expected a JSON object")
        unknown = set(data) - allowed
        if unknown:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", f"unexpected field(s): {', '.join(sorted(unknown))}")
        missing = required - set(data)
        if missing:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", f"missing field(s): {', '.join(sorted(missing))}")
        return data

    @staticmethod
    def _float(data: dict[str, object], key: str, low: float, high: float) -> float | None:
        if key not in data or data[key] is None:
            return None
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", f"{key} must be a number")
        if not low <= float(value) <= high:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", f"{key} must be between {low:g} and {high:g}")
        return float(value)

    @staticmethod
    def _g5_speed(services: "Services", fraction: float | None) -> int:
        """A speed fraction mapped onto the G5 scale; None means the ceiling."""
        ceiling = services.backend.max_speed
        if fraction is None:
            return ceiling
        return ptz.speed_from_normalised(fraction, ceiling)

    def _dispatch(self, services: "Services", target: Position,
                  speed: int | None = None) -> tuple[HTTPStatus, dict[str, object]]:
        if not services.backend.move_absolute(target, services.backend.max_speed if speed is None else speed):
            refusal = services.backend.move_refusal()
            code = "ptz_busy" if "moving" in refusal else "ptz_unavailable"
            raise ApiError(HTTPStatus.CONFLICT, code, refusal,
                           "Wait for the current move to finish." if code == "ptz_busy"
                           else "Use Recover PTZ, or check the camera connection.")
        camera = services.backend.camera()
        self._wake()
        return HTTPStatus.ACCEPTED, {
            "ok": True,
            "target": _normalised(camera, target) if camera is not None else None,
        }

    @staticmethod
    def _relative_target(camera: Camera, pan: float, tilt: float, zoom: float) -> Position:
        """Deltas as fractions of each axis span; tilt +1 = up (raw decreases)."""
        current = camera.motion.position
        span_pan = camera.pan_range.maximum - camera.pan_range.minimum
        span_tilt = camera.tilt_range.maximum - camera.tilt_range.minimum
        span_zoom = camera.zoom_range.maximum - camera.zoom_range.minimum
        return Position(
            pan=camera.pan_range.clamp(round(current.pan + pan * span_pan)),
            tilt=camera.tilt_range.clamp(round(current.tilt - tilt * span_tilt)),
            zoom=camera.zoom_range.clamp(round(current.zoom + zoom * span_zoom)),
            focus=current.focus,
        )

    @staticmethod
    def _fov_target(camera: Camera, x: float, y: float, zoom: float) -> Position:
        """Image-relative offset (x right, y up, -1..1 = frame edge) to a target."""
        geometry = camera.field_of_view
        if geometry is None:
            raise ApiError(HTTPStatus.CONFLICT, "not_calibrated",
                           "click-to-center needs a field-of-view calibration for this camera model",
                           "Use the joystick or arrow keys instead.")
        current = camera.motion.position
        factor = (camera.zoom_range.to_normalised(current.zoom) + 1.0) / 2.0
        horizontal, vertical = geometry.at_zoom(factor)
        span_pan = camera.pan_range.maximum - camera.pan_range.minimum
        span_tilt = camera.tilt_range.maximum - camera.tilt_range.minimum
        span_zoom = camera.zoom_range.maximum - camera.zoom_range.minimum
        pan_delta = x * horizontal / 2.0 * span_pan / geometry.pan_degrees
        tilt_delta = y * vertical / 2.0 * span_tilt / geometry.tilt_degrees
        return Position(
            pan=camera.pan_range.clamp(round(current.pan + pan_delta)),
            tilt=camera.tilt_range.clamp(round(current.tilt - tilt_delta)),
            zoom=camera.zoom_range.clamp(round(current.zoom + zoom * span_zoom)),
            focus=current.focus,
        )

    def ptz_move(self, services: "Services", data: object) -> tuple[HTTPStatus, dict[str, object]]:
        fields = self._fields(data, {"mode", "pan", "tilt", "zoom", "speed"}, {"mode"})
        mode = fields["mode"]
        camera = self._ptz_camera(services)
        # The optional speed is a fraction of the configured ceiling (ONVIF
        # GenericSpeedSpace); absent means full speed.
        speed_fraction = self._float(fields, "speed", 0.0, 1.0)
        speed = self._g5_speed(services, speed_fraction)
        if mode == "relative":
            pan = self._float(fields, "pan", -1.0, 1.0) or 0.0
            tilt = self._float(fields, "tilt", -1.0, 1.0) or 0.0
            zoom = self._float(fields, "zoom", -1.0, 1.0) or 0.0
            target = self._relative_target(camera, pan, tilt, zoom)
        elif mode == "continuous":
            pan = self._float(fields, "pan", -1.0, 1.0) or 0.0
            tilt = self._float(fields, "tilt", -1.0, 1.0) or 0.0
            zoom = self._float(fields, "zoom", -1.0, 1.0) or 0.0
            scale = 1.0 if speed_fraction is None else speed_fraction
            # The camera has no continuous verb: velocity becomes one bounded
            # step, so a held joystick re-issues it as each step completes.
            zoom_step = zoom * CONTINUOUS_ZOOM_FRACTION * scale
            if camera.field_of_view is not None:
                target = self._fov_target(camera, pan * 2 * CONTINUOUS_FOV_FRACTION * scale,
                                          tilt * 2 * CONTINUOUS_FOV_FRACTION * scale, zoom_step)
            else:
                target = self._relative_target(camera, pan * CONTINUOUS_SPAN_FRACTION * scale,
                                               tilt * CONTINUOUS_SPAN_FRACTION * scale, zoom_step)
        elif mode == "absolute":
            current = camera.motion.position
            pan_value = self._float(fields, "pan", -1.0, 1.0)
            tilt_value = self._float(fields, "tilt", -1.0, 1.0)
            zoom_value = self._float(fields, "zoom", 0.0, 1.0)
            target = Position(
                pan=current.pan if pan_value is None else camera.pan_range.from_normalised(pan_value),
                tilt=current.tilt if tilt_value is None else camera.tilt_range.from_normalised(-tilt_value),
                zoom=current.zoom if zoom_value is None else camera.zoom_range.from_normalised(zoom_value * 2 - 1),
                focus=current.focus,
            )
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", "mode must be relative, absolute or continuous")
        return self._dispatch(services, target, speed)

    def ptz_center(self, services: "Services", data: object) -> tuple[HTTPStatus, dict[str, object]]:
        fields = self._fields(data, {"x", "y", "zoom"}, {"x", "y"})
        x = self._float(fields, "x", -1.0, 1.0) or 0.0
        y = self._float(fields, "y", -1.0, 1.0) or 0.0
        zoom = self._float(fields, "zoom", -1.0, 1.0) or 0.0
        camera = self._ptz_camera(services)
        return self._dispatch(services, self._fov_target(camera, x, y, zoom))

    def ptz_stop(self, services: "Services") -> tuple[HTTPStatus, dict[str, object]]:
        self._ptz_camera(services)
        # Every step completes on its own; stopping means "send nothing more"
        # (the client) plus a fresh position report (here).
        polled = services.backend.refresh_position()
        self._wake()
        return HTTPStatus.ACCEPTED, {"ok": True, "polled": bool(polled)}

    def ptz_home(self, services: "Services") -> tuple[HTTPStatus, dict[str, object]]:
        camera = self._ptz_camera(services)
        home = next((p for p in camera.presets.values() if p.name.strip().casefold() == "home"), None)
        if home is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "no_home", "no Home position is saved",
                           "Save the current view as Home first.")
        return self.preset_goto(services, home.index)

    def ptz_recover(self, services: "Services") -> tuple[HTTPStatus, dict[str, object]]:
        """Recover a stuck PTZ state (motor timeout / lost channel).

        Order of preference: an installed :attr:`recover_hook`, a backend
        ``recover_ptz()`` callable (the PTZ watchdog contract), and finally a
        position poll, whose motor-state reply clears a timeout.
        """
        self._ptz_camera(services)
        accepted: bool
        message: str
        if self.recover_hook is not None:
            accepted, message = self.recover_hook(services)
        else:
            hook = getattr(services.backend, "recover_ptz", None)
            if callable(hook):
                value = hook()
                if isinstance(value, tuple):
                    accepted, message = bool(value[0]), str(value[1])
                else:
                    accepted, message = bool(value), "PTZ recovery requested"
            else:
                accepted = bool(services.backend.refresh_position())
                message = "Position poll sent; the state clears when the motor replies"
        self._wake()
        if not accepted:
            raise ApiError(HTTPStatus.CONFLICT, "recover_refused",
                           message if message else "the PTZ channel did not accept a recovery request",
                           "Check that the camera is connected and adopted.")
        log.info("operator requested PTZ recovery: %s", message)
        return HTTPStatus.ACCEPTED, {"ok": True, "message": message}

    # ------------------------------------------------------------ presets

    def _thumb_key(self, services: "Services", token: int) -> tuple[int, int]:
        return id(services), token

    def _thumb(self, services: "Services", token: int) -> bytes | None:
        with self._lock:
            return self._thumbs.get(self._thumb_key(services, token))

    def _has_thumb(self, services: "Services") -> Callable[[int], bool]:
        def has(token: int) -> bool:
            with self._lock:
                return self._thumb_key(services, token) in self._thumbs
        return has

    def _capture_thumbnail(self, services: "Services", token: int) -> None:
        try:
            image = services.backend.snapshot()
        except Exception:  # pragma: no cover - thumbnails are best effort
            image = None
        if not image or len(image) > MAX_THUMBNAIL_BYTES:
            return
        with self._lock:
            key = self._thumb_key(services, token)
            self._thumbs.pop(key, None)
            self._thumbs[key] = image
            while len(self._thumbs) > MAX_THUMBNAILS:
                self._thumbs.popitem(last=False)

    def _presets(self, services: "Services") -> list[dict[str, object]]:
        return preset_list(services.backend.camera(), self._has_thumb(services))

    @staticmethod
    def _from_legacy(status: HTTPStatus, result: dict[str, object]) -> dict[str, object]:
        if status < 400:
            return result
        error = result.get("error")
        code = {
            HTTPStatus.BAD_REQUEST: "invalid", HTTPStatus.NOT_FOUND: "not_found",
            HTTPStatus.CONFLICT: "conflict", HTTPStatus.SERVICE_UNAVAILABLE: "unavailable",
        }.get(status, "error")
        raise ApiError(status, code, str(error or "request failed"))

    def preset_save(self, services: "Services", data: object) -> tuple[HTTPStatus, dict[str, object]]:
        fields = self._fields(data, {"name"}, {"name"})
        name = fields["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > _PRESET_NAME_MAX:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", "name must be 1-64 characters")
        self._ptz_camera(services)
        status, result = services.control_preset({"action": "save", "name": name.strip()})
        result = self._from_legacy(status, result)
        self._capture_thumbnail(services, int(str(result["token"])))
        self._emit_presets()
        return HTTPStatus.CREATED, {"ok": True, "preset": self._preset_by_token(services, int(str(result["token"])))}

    def _preset_by_token(self, services: "Services", token: int) -> dict[str, object] | None:
        return next((p for p in self._presets(services) if p["token"] == token), None)

    def _named(self, services: "Services", token: int) -> str:
        camera = self._ptz_camera(services)
        preset = camera.presets.get(token)
        if preset is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "no_preset", "no such preset")
        return preset.name

    def preset_update(self, services: "Services", token: int) -> tuple[HTTPStatus, dict[str, object]]:
        name = self._named(services, token)
        status, result = services.control_preset({"action": "save", "name": name})
        self._from_legacy(status, result)
        self._capture_thumbnail(services, token)
        self._emit_presets()
        return HTTPStatus.OK, {"ok": True, "preset": self._preset_by_token(services, token)}

    def preset_delete(self, services: "Services", token: int) -> tuple[HTTPStatus, dict[str, object]]:
        self._named(services, token)
        if not services.backend.remove_preset(token):
            raise ApiError(HTTPStatus.CONFLICT, "conflict", "the camera did not remove the preset")
        with self._lock:
            self._thumbs.pop(self._thumb_key(services, token), None)
        self._emit_presets()
        return HTTPStatus.OK, {"ok": True, "token": token}

    def preset_goto(self, services: "Services", token: int) -> tuple[HTTPStatus, dict[str, object]]:
        self._named(services, token)
        if not services.backend.goto_preset(token, services.backend.max_speed):
            refusal = services.backend.move_refusal()
            raise ApiError(HTTPStatus.CONFLICT, "ptz_busy" if "moving" in refusal else "ptz_unavailable", refusal)
        self._wake()
        return HTTPStatus.ACCEPTED, {"ok": True, "token": token}

    # --------------------------------------------------------------- logs

    def _logs(self, query: dict[str, list[str]]) -> dict[str, object]:
        services = self.server.services

        def integer(name: str, default: int, low: int, high: int) -> int:
            raw = query.get(name, [str(default)])[0]
            try:
                value = int(raw)
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", f"{name} must be an integer") from None
            if not low <= value <= high:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", f"{name} must be between {low} and {high}")
            return value

        limit = integer("limit", 100, 1, 500)
        minimum = _log_level(query.get("level", ["INFO"])[0])
        before = query.get("before")
        if before:
            entries = services.logs.before(integer("before", 0, 0, 2**62), limit, minimum)
        else:
            entries = services.logs.entries(limit, minimum)
        summary = services.logs.summary()
        return {"entries": entries, "warnings": summary["warnings"], "errors": summary["errors"],
                "last_seq": services.logs.last_seq}

    # ------------------------------------------------------------ session

    @staticmethod
    def _peer(h: "_Handler") -> str:
        return h.client_address[0] if h.client_address else ""

    def _session_body(self, h: "_Handler") -> dict[str, object]:
        auth = self.server.services.auth
        principal = h._session()
        kind = None
        if principal is not None:
            kind = principal[0] if principal[0] in ("anonymous", "ingress") else "session"
        return {
            "mode": auth.mode,
            "authenticated": principal is not None,
            "principal": kind,
            "csrf": principal[1] if principal is not None else None,
            "ingress": bool(websecurity.ingress_prefix(h.headers)),
        }

    def _login(self, h: "_Handler") -> None:
        auth = self.server.services.auth
        data = self._read_json(h)
        fields = self._fields(data, {"password"}, {"password"})
        password = fields["password"]
        if auth.mode != "password":
            raise ApiError(HTTPStatus.CONFLICT, "auth_mode",
                           "password sign-in is not configured" if auth.mode == "locked"
                           else "authentication is disabled")
        if not isinstance(password, str) or len(password) > 1024:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", "password must be a string")
        outcome = h.attempt_login(password)
        if isinstance(outcome, float):
            wait = max(1, math.ceil(outcome))
            body = ApiError(HTTPStatus.TOO_MANY_REQUESTS, "throttled",
                            f"too many attempts; try again in {wait} s").body()
            self._send_json(h, HTTPStatus.TOO_MANY_REQUESTS, body, headers=(("Retry-After", str(wait)),))
            return
        if outcome is None:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "bad_password", "incorrect password")
        self._send_json(h, HTTPStatus.OK,
                        {"mode": auth.mode, "authenticated": True, "principal": "session", "csrf": outcome[1]},
                        headers=(("Set-Cookie", h.session_cookie(outcome[0], max_age=auth.SESSION_TTL_SEC)),))

    def _logout(self, h: "_Handler") -> None:
        principal = self._authorize(h, csrf=True)
        self._drain(h)
        if principal[0] not in ("anonymous", "ingress"):
            self.server.services.auth.logout(principal[0])
        self._send_json(h, HTTPStatus.OK, {"authenticated": False},
                        headers=(("Set-Cookie", h.session_cookie("", max_age=0)),))

    def _authorize(self, h: "_Handler", *, csrf: bool) -> tuple[str, str]:
        auth = self.server.services.auth
        if auth.mode == "locked":
            self._drain(h)
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "auth_not_configured",
                           "the operator console is locked because no admin password is configured",
                           "Set OSPREY_ADMIN_PASSWORD_FILE (or OSPREY_ADMIN_PASSWORD_HASH) and restart.")
        principal = h._session()
        if principal is None:
            self._drain(h)
            raise ApiError(HTTPStatus.UNAUTHORIZED, "login_required", "sign in to continue")
        if csrf:
            supplied = h.headers.get("X-CSRF-Token", "")
            if not supplied or not hmac.compare_digest(supplied, principal[1]):
                self._drain(h)
                raise ApiError(HTTPStatus.FORBIDDEN, "csrf", "missing or invalid X-CSRF-Token header")
        return principal

    # ------------------------------------------------------------- bodies

    @staticmethod
    def _drain(h: "_Handler") -> None:
        if getattr(h, "_web_body_read", False):
            return
        h._web_body_read = True  # type: ignore[attr-defined]
        h._drain()

    def _read_json(self, h: "_Handler") -> object:
        try:
            length = int(h.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_JSON_BYTES:
            h.close_connection = True
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "request body is too large")
        raw = h.rfile.read(length) if length else b""
        h._web_body_read = True  # type: ignore[attr-defined]
        if h.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type", "Content-Type must be application/json")
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "request body is not valid JSON") from None

    def _send_json(self, h: "_Handler", status: HTTPStatus, body: object,
                   headers: tuple[tuple[str, str], ...] = ()) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        h._send(status, payload, "application/json", headers=(("Cache-Control", "no-store"), *headers))

    def _send_error(self, h: "_Handler", error: ApiError) -> None:
        self._drain(h)
        self._send_json(h, error.status, error.body())

    # ------------------------------------------------------------- static

    def _load(self, relative: str) -> tuple[bytes, str] | None:
        if not _STATIC_NAME.fullmatch(relative) or ".." in relative.split("/"):
            return None
        path = (self.web_root / relative).resolve()
        try:
            path.relative_to(self.web_root.resolve())
        except ValueError:
            return None
        content_type = _STATIC_TYPES.get(path.suffix.lower())
        if content_type is None or not path.is_file():
            return None
        try:
            mtime = path.stat().st_mtime
            cached = self._static.get(relative)
            if cached is not None and cached[0] == mtime:
                return cached[1], content_type
            data = path.read_bytes()
        except OSError:
            return None
        self._static[relative] = (mtime, data, content_type)
        return data, content_type

    def _serve_static(self, h: "_Handler", relative: str) -> None:
        loaded = self._load(relative)
        if loaded is None:
            h._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        data, content_type = loaded
        etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
        headers = (("Cache-Control", "no-cache"), ("ETag", etag))
        if h.headers.get("If-None-Match", "") == etag:
            h._send(HTTPStatus.NOT_MODIFIED, b"", content_type, headers=headers)
            return
        h._send(HTTPStatus.OK, data, content_type, headers=headers)

    def _serve_index(self, h: "_Handler") -> None:
        loaded = self._load("index.html")
        if loaded is None:
            h._send(HTTPStatus.NOT_FOUND, b"web UI is not installed", "text/plain")
            return
        data, content_type = loaded
        hashes = tuple(
            base64.b64encode(hashlib.sha256(block.encode("utf-8")).digest()).decode()
            for block in _IMPORT_MAP.findall(data.decode("utf-8"))
        )
        h.send_response(HTTPStatus.OK)
        h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.send_security_headers(script_hashes=hashes)
        h.end_headers()
        h.wfile.write(data)

    # ---------------------------------------------------------------- SSE

    @contextmanager
    def _sse_slot(self) -> Iterator[bool]:
        with self._lock:
            allowed = self._sse_clients < self.max_sse_clients
            if allowed:
                self._sse_clients += 1
        try:
            yield allowed
        finally:
            if allowed:
                with self._lock:
                    self._sse_clients -= 1

    def _wake(self) -> None:
        """Hint that state changed (reserved for push-based producers)."""

    def _emit_presets(self) -> None:
        """Preset changes are picked up by each stream's diff; kept as a seam."""

    def _events(self, request: _Request) -> None:
        h = request.handler
        query = request.query
        camera_id = query.get("camera", [DEFAULT_CAMERA])[0]
        services = self._resolve(camera_id)
        minimum = _log_level(query.get("log_level", ["INFO"])[0])
        last_seq_text = h.headers.get("Last-Event-ID", "") or query.get("last_event_id", [""])[0]
        try:
            last_seq = int(last_seq_text) if last_seq_text else None
        except ValueError:
            last_seq = None
        with self._sse_slot() as allowed:
            if not allowed:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "busy", "too many live connections",
                               "Close other Osprey tabs; the page falls back to polling.")
            h.close_connection = True
            h.send_response(HTTPStatus.OK)
            h.send_header("Content-Type", "text/event-stream; charset=utf-8")
            h.send_header("Cache-Control", "no-store")
            h.send_header("X-Accel-Buffering", "no")
            h.send_header("Connection", "close")
            h.send_security_headers()
            h.end_headers()
            try:
                self._stream(h, camera_id, services, minimum, last_seq)
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass

    def _stream(self, h: "_Handler", camera_id: str, services: "Services",
                minimum: int, last_seq: int | None) -> None:
        def write(chunk: bytes) -> None:
            h.wfile.write(chunk)
            h.wfile.flush()

        logs = services.logs
        state = self.state(camera_id, services)
        write(b"retry: 3000\n\n")
        write(sse_frame("state", state))
        seq = logs.last_seq if last_seq is None else last_seq
        last_ptz = state["ptz"]
        last_health = state["health"]
        last_presets = state["presets"]
        last_adopted = state["camera"] is not None and bool(
            isinstance(state["camera"], dict) and state["camera"].get("adopted"))
        now = time.monotonic()
        last_write = now
        last_streams = now
        last_auth = now
        cookie = h.headers.get("Cookie", "")
        peer = self._peer(h)
        ingress = bool(websecurity.ingress_prefix(h.headers))
        while not self._stopping.wait(self.poll_interval):
            now = time.monotonic()
            frames: list[bytes] = []
            camera = services.backend.camera()
            ptz = ptz_state(camera, enabled=services.ptz_enabled)
            if _changed(ptz, last_ptz, ignore=("updated_at",)):
                frames.append(sse_frame("ptz", ptz))
                last_ptz = ptz
            for entry in logs.since(seq, minimum, limit=200):
                seq = max(seq, int(str(entry.get("seq", seq))))
                frames.append(sse_frame("log", entry, event_id=seq))
            adopted = camera is not None and camera.adopted
            if adopted != last_adopted:
                frames.append(sse_frame("adoption", {"adopted": adopted, "camera": self._camera_summary(camera_id, services)}))
                last_adopted = adopted
            presets = self._presets(services)
            if presets != last_presets:
                frames.append(sse_frame("preset", {"presets": presets}))
                last_presets = presets
            if now - last_streams >= self.streams_interval:
                last_streams = now
                frames.append(sse_frame("streams", stream_stats(services)))
                health = {"checks": health_checks(services)}
                if health != last_health:
                    frames.append(sse_frame("health", health))
                    last_health = health
            if now - last_auth >= min(self.heartbeat_interval, 5.0):
                last_auth = now
                if services.auth.principal(cookie, peer=peer, ingress=ingress) is None:
                    write(sse_frame("session", {"authenticated": False}))
                    return
            if not frames and now - last_write >= self.heartbeat_interval:
                frames.append(sse_comment("ping"))
            if frames:
                write(b"".join(frames))
                last_write = now


def _changed(new: dict[str, object], old: object, ignore: tuple[str, ...] = ()) -> bool:
    if not isinstance(old, dict):
        return True
    keys = (set(new) | set(old)) - set(ignore)
    return any(new.get(k) != old.get(k) for k in keys)


def _log_level(name: str) -> int:
    level = logging.getLevelNamesMapping().get(name.upper())
    if level is None or level < logging.INFO or level > logging.CRITICAL:
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid", "level must be INFO, WARNING or ERROR")
    return level
