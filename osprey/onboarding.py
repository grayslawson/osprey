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

import manage

log = logging.getLogger("osprey.onboarding")

DEFAULT_CAMERA_LOGIN = "ubnt"  # a factory-fresh or reset camera's user and password


class OnboardingError(ValueError):
    """A request the wizard can show back to the user as is."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


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
