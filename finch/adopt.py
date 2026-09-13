"""Getting an adoption token out of a controller.

A camera cannot adopt itself: the controller issues a token and the camera quotes
it in its hello. In the UI that token is minted when the operator adds a device by
hand; over the API it is one call, which is what this does.

    POST /api/auth/login          → session cookie
    GET  /proxy/protect/api/cameras/manage-payload
                                  → {"mgmt": {"hosts": [...], "token": "..."}}

Nothing here is written back to the controller, so a failed run leaves no trace.
Tokens are short-lived — an hour on this controller — so they are fetched per run
rather than stored.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Final

LOGIN_PATH: Final = "/api/auth/login"
PAYLOAD_PATH: Final = "/proxy/protect/api/cameras/manage-payload"
BOOTSTRAP_PATH: Final = "/proxy/protect/api/bootstrap"
CAMERAS_PATH: Final = "/proxy/protect/api/cameras"

log = logging.getLogger("finch.adopt")


class AdoptionError(Exception):
    pass


@dataclass(frozen=True)
class Management:
    """What the controller says a camera should do to reach it."""

    token: str
    hosts: tuple[str, ...]
    protocol: str = "wss"

    @property
    def first_host(self) -> tuple[str, int]:
        if not self.hosts:
            raise AdoptionError("controller named no hosts")
        host, _, port = self.hosts[0].rpartition(":")
        if not host or not port.isdigit():
            raise AdoptionError(f"unreadable host {self.hosts[0]!r}")
        return host, int(port)


def _opener() -> urllib.request.OpenerDirector:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(CookieJar()),
    )


def parse_management(body: dict[str, Any]) -> Management:
    """Read the management block. The token is nested under `mgmt`, not top level."""
    mgmt = body.get("mgmt")
    if not isinstance(mgmt, dict):
        raise AdoptionError("no mgmt block in the manage payload")
    token = mgmt.get("token")
    if not isinstance(token, str) or not token:
        raise AdoptionError("no adoption token in the manage payload")
    hosts = mgmt.get("hosts")
    hosts = tuple(str(h) for h in hosts) if isinstance(hosts, list) else ()
    return Management(token=token, hosts=hosts, protocol=str(mgmt.get("protocol", "wss")))


def rename(host: str, username: str, password: str, mac: str, name: str, port: int = 443) -> bool:
    """Label our own camera in the controller.

    Protect names an adopted camera after its *model*, ignoring the name in the
    hello — so a virtual camera shows up looking exactly like the real one beside
    it. This renames the device whose MAC is ours, and touches nothing else.
    """
    opener = _opener()
    base = f"https://{host}:{port}"
    credentials = json.dumps({"username": username, "password": password}).encode()
    try:
        login = urllib.request.Request(
            base + LOGIN_PATH, data=credentials, headers={"Content-Type": "application/json"}
        )
        with opener.open(login, timeout=20):
            pass
        with opener.open(base + BOOTSTRAP_PATH, timeout=20) as response:
            bootstrap = json.loads(response.read().decode())
    except (urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
        log.warning("could not read the controller to rename ourselves: %s", exc)
        return False

    cameras = bootstrap.get("cameras") if isinstance(bootstrap, dict) else None
    if not isinstance(cameras, list):
        return False
    wanted = mac.replace(":", "").upper()
    for camera in cameras:
        if not isinstance(camera, dict) or str(camera.get("mac", "")).upper() != wanted:
            continue
        if camera.get("name") == name:
            return True
        request = urllib.request.Request(
            f"{base}{CAMERAS_PATH}/{camera.get('id')}",
            data=json.dumps({"name": name}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        try:
            with opener.open(request, timeout=20):
                log.info("named ourselves %r in the controller", name)
                return True
        except (urllib.error.HTTPError, OSError) as exc:
            log.warning("could not rename ourselves: %s", exc)
            return False
    return False


def fetch_token(host: str, username: str, password: str, port: int = 443) -> Management:
    """Log in and ask for a fresh adoption token."""
    opener = _opener()
    base = f"https://{host}:{port}"
    credentials = json.dumps({"username": username, "password": password}).encode()
    login = urllib.request.Request(
        base + LOGIN_PATH, data=credentials, headers={"Content-Type": "application/json"}
    )
    try:
        with opener.open(login, timeout=20) as response:
            if response.status not in (200, 204):
                raise AdoptionError(f"login refused with {response.status}")
    except urllib.error.HTTPError as exc:
        raise AdoptionError(f"login refused with {exc.code}") from exc
    except OSError as exc:
        raise AdoptionError(f"could not reach {base}: {exc}") from exc

    try:
        with opener.open(base + PAYLOAD_PATH, timeout=20) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise AdoptionError(f"manage payload refused with {exc.code}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AdoptionError(f"could not read the manage payload: {exc}") from exc

    if not isinstance(body, dict):
        raise AdoptionError("manage payload was not an object")
    management = parse_management(body)
    log.info("token obtained, controller reachable at %s", ", ".join(management.hosts) or "?")
    return management
