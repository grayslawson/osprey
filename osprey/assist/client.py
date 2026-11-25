"""Assist's view of Osprey: the HTTP API with the Assist bearer token, nothing else."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol


class OspreyError(Exception):
    """Osprey refused or could not be reached. ``status`` 0 means no answer at all."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message

    @property
    def busy(self) -> bool:
        return self.status == 409 and self.code == "ptz_busy"


class Camera(Protocol):
    """What the engine needs from a camera; tests supply a fake with the same methods."""

    camera_id: str

    def state(self) -> dict[str, Any]: ...
    def sentry(self) -> dict[str, Any]: ...
    def snapshot(self) -> bytes: ...
    def presets(self) -> list[dict[str, Any]]: ...
    def goto(self, token: int) -> None: ...
    def move_relative(self, pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0) -> None: ...
    def move_absolute(self, pan: float | None = None, tilt: float | None = None,
                      zoom: float | None = None, speed: float | None = None) -> None: ...
    def isp(self, settings: dict[str, float]) -> bool | None: ...
    def delete_preset(self, token: int) -> None: ...
    def set_tour(self, points: list[dict[str, Any]], enabled: bool | None = None) -> None: ...
    def set_dwell(self, dwell: dict[str, float], ttl_sec: float) -> None: ...
    def save_preset(self, name: str) -> dict[str, Any]: ...
    def set_night_view(self, point: str, preset: str | None) -> None: ...
    def hold(self, seconds: float, reason: str) -> bool: ...
    def release(self) -> None: ...


class OspreyClient:
    def __init__(self, url: str, token_file: str, camera_id: str = "default", timeout: float = 20.0) -> None:
        self.url = url.rstrip("/")
        self.token_file = token_file
        self.camera_id = camera_id
        self.timeout = timeout

    def _token(self) -> str:
        try:
            return Path(self.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise OspreyError(0, "no_token", f"cannot read the Assist token file: {type(exc).__name__}") from None

    def _call(self, method: str, path: str, body: object = None, *, raw: bool = False) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + "/api/v1" + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token()}",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:  # noqa: S310 - operator-set URL
                payload = reply.read()
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read()).get("error", {})
            except (ValueError, AttributeError):
                error = {}
            raise OspreyError(exc.code, str(error.get("code", "http")), str(error.get("message", exc.reason))) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OspreyError(0, "unreachable", type(exc).__name__) from None
        if raw:
            return payload
        try:
            return json.loads(payload) if payload else {}
        except ValueError:
            raise OspreyError(0, "bad_reply", "Osprey sent something that is not JSON") from None

    def _cam(self, rest: str) -> str:
        return f"/cameras/{urllib.parse.quote(self.camera_id, safe='')}{rest}"

    def state(self) -> dict[str, Any]:
        return dict(self._call("GET", self._cam("/state")))

    def sentry(self) -> dict[str, Any]:
        try:
            return dict(self._call("GET", self._cam("/sentry")))
        except OspreyError as exc:
            if exc.code == "no_sentry":
                return {}
            raise

    def snapshot(self) -> bytes:
        return bytes(self._call("GET", self._cam("/snapshot.jpg"), raw=True))

    def presets(self) -> list[dict[str, Any]]:
        return list(self._call("GET", self._cam("/presets")).get("presets", []))

    def goto(self, token: int) -> None:
        self._call("POST", self._cam(f"/presets/{int(token)}/goto"), {})

    def move_relative(self, pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0) -> None:
        self._call("POST", self._cam("/ptz/move"), {"mode": "relative", "pan": pan, "tilt": tilt, "zoom": zoom})

    def move_absolute(self, pan: float | None = None, tilt: float | None = None,
                      zoom: float | None = None, speed: float | None = None) -> None:
        body: dict[str, Any] = {"mode": "absolute"}
        for key, value in (("pan", pan), ("tilt", tilt), ("zoom", zoom)):
            if value is not None:
                body[key] = round(float(value), 5)
        if speed is not None:
            # A fraction of the configured ceiling. One is the ceiling itself,
            # so leaving it out and sending one mean the same move.
            body["speed"] = round(float(speed), 5)
        self._call("POST", self._cam("/ptz/move"), body)

    def delete_preset(self, token: int) -> None:
        self._call("DELETE", self._cam(f"/presets/{int(token)}"))

    def set_tour(self, points: list[dict[str, Any]], enabled: bool | None = None) -> None:
        """Replace the patrol's points (and optionally switch it on or off). Needs the patrol scope."""
        body: dict[str, Any] = {"points": points}
        if enabled is not None:
            body["enabled"] = bool(enabled)
        self._call("POST", self._cam("/sentry"), body)

    def set_dwell(self, dwell: dict[str, float], ttl_sec: float) -> None:
        self._call("POST", self._cam("/sentry/dwell"), {"dwell_sec": dwell, "ttl_sec": ttl_sec})

    def isp(self, settings: dict[str, float]) -> bool | None:
        reply = self._call("POST", self._cam("/isp"), settings)
        acked = reply.get("acked")
        return None if acked is None else bool(acked)

    def save_preset(self, name: str) -> dict[str, Any]:
        return dict(self._call("POST", self._cam("/presets"), {"name": name}).get("preset") or {})

    def set_night_view(self, point: str, preset: str | None) -> None:
        """Set (or clear) one patrol point's night view. The rest of the tour is sent back unchanged."""
        points = [dict(p) for p in (self.sentry().get("config") or {}).get("points", [])]
        for entry in points:
            if str(entry.get("name", "")).casefold() == point.casefold():
                if preset:
                    entry["night_preset"] = preset
                else:
                    entry.pop("night_preset", None)
                break
        else:
            raise OspreyError(404, "no_point", f"{point} is not a patrol point")
        self._call("POST", self._cam("/sentry"), {"points": points})

    def hold(self, seconds: float, reason: str) -> bool:
        return bool(self._call("POST", self._cam("/sentry/hold"), {"seconds": seconds, "reason": reason}).get("held"))

    def release(self) -> None:
        self._call("DELETE", self._cam("/sentry/hold"))
