"""The setup wizard's steps after the configuration is saved.

They run on the restarted daemon behind the normal admin session and CSRF
token (``/api/v1/setup/...``), so they also work later from the console.
Adopting is what ``osprey/manage.sh`` does: log in to the camera's HTTPS API
and point it at this controller's control port.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from pathlib import Path

import frigate_setup
import manage
from frigate_config import validate_url

log = logging.getLogger("osprey.onboarding")

DEFAULT_CAMERA_LOGIN = "ubnt"  # a factory-fresh or reset camera's user and password


class OnboardingError(ValueError):
    """A request the wizard can show back to the user as is."""

    def __init__(self, message: str, status: int = 400, code: str = "setup") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _text(data: dict[str, object], key: str, default: str, limit: int) -> str:
    value = data.get(key, default)
    if value is None or value == "":
        value = default
    if not isinstance(value, str) or len(value) > limit:
        raise OnboardingError(f"{key} must be text of at most {limit} characters")
    return value


@dataclass
class Onboarding:
    host: str
    control_port: int
    cameras: tuple[dict[str, object], ...] = ()
    camera_port: int = manage.CAMERA_HTTPS_PORT
    rtsp_port: int = 8554
    onvif_port: int = 8000
    rtsp_login: tuple[str, str] | None = None
    onvif_login: tuple[str, str] | None = None
    state_dir: Path = Path(".")

    def status(self, frigate_url: str | None) -> dict[str, object]:
        """What the wizard needs to resume after a reload: the saved camera and Frigate."""
        return {
            "host": self.host,
            "cameras": [{k: c.get(k) for k in ("mac", "ip", "name")} for c in self.cameras],
            "frigate_url": frigate_url or "",
        }

    def adopt(self, data: object) -> dict[str, object]:
        """Point the camera at Osprey; it then dials the control port and adopts."""
        if not isinstance(data, dict):
            raise OnboardingError("request body must be an object")
        ip = _text(data, "ip", "", 64).strip()
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            raise OnboardingError("camera IP address must be a valid IP address") from None
        username = _text(data, "username", DEFAULT_CAMERA_LOGIN, 64)
        password = _text(data, "password", DEFAULT_CAMERA_LOGIN, 256)
        log.info("setup: pointing the camera at %s at %s:%d", ip, self.host, self.control_port)
        try:
            manage.take_custody(ip, username, password, self.host, "osprey",
                                self.control_port, self.camera_port)
        except manage.ManageError as exc:
            raise OnboardingError(str(exc), status=502) from exc
        return {"ok": True, "message": f"The camera at {ip} accepted Osprey's address and is connecting."}

    # ------------------------------------------------------------- Frigate

    def _camera_name(self, data: dict[str, object]) -> str:
        fallback = str(self.cameras[0].get("name") or "") if self.cameras else ""
        return frigate_setup.camera_name(_text(data, "name", fallback, 64))

    def frigate_block(self, data: dict[str, object]) -> dict[str, object]:
        """The camera entry with this Osprey's address, ports and credentials filled in.

        It holds the ONVIF and RTSP passwords, so only the admin session sees it.
        """
        name = self._camera_name(data)
        block = frigate_setup.camera_block(name, self.host, self.rtsp_port, self.onvif_port,
                                           self.rtsp_login, self.onvif_login)
        return {"name": name, "yaml": "cameras:\n" + block, "block": block}

    def _frigate(self, data: object, saved_url: str | None) -> tuple[dict[str, object], frigate_setup.Frigate]:
        if not isinstance(data, dict):
            raise OnboardingError("request body must be an object")
        url = _text(data, "url", saved_url or "", 2048)
        if not url:
            raise OnboardingError("enter the Frigate URL first")
        try:
            url = validate_url(url)
        except ValueError as exc:
            raise OnboardingError(str(exc)) from None
        client = frigate_setup.Frigate(
            url, _text(data, "username", "", 128), _text(data, "password", "", 256),
            insecure_tls=data.get("insecure_tls") is True,
        )
        return data, client

    def _merged(self, data: dict[str, object], client: frigate_setup.Frigate) -> tuple[str, str, str]:
        block = self.frigate_block(data)
        try:
            raw = client.raw_config()
            return raw, frigate_setup.merge(raw, str(block["name"]), str(block["block"])), str(block["name"])
        except frigate_setup.FrigateError as exc:
            raise OnboardingError(str(exc), 409 if exc.code == "conflict" else 502, f"frigate_{exc.code}") from None

    def frigate_preview(self, data: object, saved_url: str | None) -> dict[str, object]:
        """What Add to Frigate would change, without changing it."""
        fields, client = self._frigate(data, saved_url)
        raw, merged, name = self._merged(fields, client)
        return {"name": name, "diff": frigate_setup.diff(raw, merged), "base": frigate_setup.fingerprint(raw)}

    def frigate_apply(self, data: object, saved_url: str | None) -> dict[str, object]:
        """Back up Frigate's config, then save it with the camera added."""
        fields, client = self._frigate(data, saved_url)
        raw, merged, name = self._merged(fields, client)
        if fields.get("base") != frigate_setup.fingerprint(raw):
            raise OnboardingError("Frigate's config changed since the preview. Preview it again.",
                                  409, "frigate_changed")
        saved = frigate_setup.backup(self.state_dir, raw)
        try:
            message = client.save(merged, restart=fields.get("restart") is not False)
        except frigate_setup.FrigateError as exc:
            raise OnboardingError(f"{exc} Frigate's config was not changed; the backup is {saved}.",
                                  502, f"frigate_{exc.code}") from None
        log.info("setup: added camera %s to Frigate at %s; previous config kept in %s", name, client.url, saved)
        return {"ok": True, "name": name, "message": message, "backup": str(saved)}
