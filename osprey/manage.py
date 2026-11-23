"""Taking custody of a camera the native way — the `/api/1.2/manage` POST.

This is the step a real controller performs after discovery, and the only way to
move a camera that is not already asking to be adopted. Measured against a real
G5 PTZ and cross-read with unifi-cam-proxy's camera-side implementation:

    POST https://<camera>/api/1.2/login    {"username":…, "password":…}
        → 200 with  Set-Cookie: TOKEN=<hex>
    POST https://<camera>/api/1.2/manage    {"mgmt": {"token":…, "hosts":[…], …}}
        with that cookie
        → 200, and the camera dials the first host and says hello

Two things worth knowing:

* **The manage `token` is ours to choose.** It is the `adoptionCode` the camera
  will quote back in its hello, and Osprey is the controller — it decides what to
  accept. It does not have to come from a real console.
* **The login credential is not ours to choose.** It is the camera's current
  management credential: the default (`ubnt`/`ubnt`) only on a factory-fresh or
  reset device, otherwise whatever controller adopted it last set. Taking a camera
  from another controller therefore needs that controller's cooperation — either
  its stored credential, or a factory reset — which is a decision for whoever owns
  the camera, not something to guess.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Final

from controller import CONTROL_PORT

LOGIN_PATH: Final = "/api/1.2/login"
MANAGE_PATH: Final = "/api/1.2/manage"
CAMERA_HTTPS_PORT: Final = 443
TIMEOUT_SEC: Final = 15.0

log = logging.getLogger("osprey.manage")


class ManageError(Exception):
    pass


def _opener() -> urllib.request.OpenerDirector:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(CookieJar()),
    )


def manage_payload(controller_host: str, token: str, port: int = CONTROL_PORT) -> dict[str, Any]:
    """What tells the camera who to connect to. `hosts[0]` is dialled first."""
    return {
        "mgmt": {
            "protocol": "wss",
            "hosts": [f"{controller_host}:{port}"],
            "token": token,
        }
    }


@dataclass
class Camera:
    """One camera's management HTTPS endpoint."""

    host: str
    username: str
    password: str
    port: int = CAMERA_HTTPS_PORT

    def _base(self) -> str:
        return f"https://{self.host}:{self.port}"

    def login(self, opener: urllib.request.OpenerDirector) -> None:
        """Authenticate. The session cookie the camera sets is kept by the opener."""
        body = json.dumps({"username": self.username, "password": self.password}).encode()
        request = urllib.request.Request(
            self._base() + LOGIN_PATH, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with opener.open(request, timeout=TIMEOUT_SEC) as response:
                if response.status != 200:
                    raise ManageError(f"login returned {response.status}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ManageError(
                    "camera rejected the credential (401). It wants its current "
                    "management password — the default only on a factory-fresh device."
                ) from exc
            raise ManageError(f"login failed with {exc.code}") from exc
        except OSError as exc:
            raise ManageError(f"could not reach the camera at {self.host}: {exc}") from exc

    def manage(
        self, opener: urllib.request.OpenerDirector, controller_host: str, token: str,
        control_port: int = CONTROL_PORT,
    ) -> dict[str, Any]:
        """Hand the camera the controller address. It dials there after this."""
        body = json.dumps(manage_payload(controller_host, token, control_port)).encode()
        request = urllib.request.Request(
            self._base() + MANAGE_PATH, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with opener.open(request, timeout=TIMEOUT_SEC) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise ManageError(f"manage failed with {exc.code}: {exc.read()[:200]!r}") from exc
        except OSError as exc:
            raise ManageError(f"manage could not reach the camera: {exc}") from exc
        try:
            return dict(json.loads(raw)) if raw else {}
        except json.JSONDecodeError:
            return {"raw": raw}


def take_custody(
    camera_host: str,
    username: str,
    password: str,
    controller_host: str,
    token: str,
    control_port: int = CONTROL_PORT,
    camera_port: int = CAMERA_HTTPS_PORT,
) -> dict[str, Any]:
    """Log in, then point the camera at the controller. It dials there and adopts."""
    camera = Camera(host=camera_host, username=username, password=password, port=camera_port)
    opener = _opener()
    log.info("authenticating to the camera at %s", camera_host)
    camera.login(opener)
    log.info("pointing %s at %s:%d", camera_host, controller_host, control_port)
    result = camera.manage(opener, controller_host, token, control_port)
    log.info("manage accepted; the camera will dial us and say hello")
    return result


def _configured() -> dict[str, Any]:
    """Osprey's own config file, for defaults when run next to it (``osprey adopt``)."""
    path = os.environ.get("OSPREY_CONFIG_FILE")
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Take custody of a camera by pointing it at this controller."
    )
    parser.add_argument("--camera", required=True, help="the camera's address")
    parser.add_argument("--user", default="ubnt",
                        help="the camera's management username (default ubnt, a reset camera's)")
    pw = parser.add_mutually_exclusive_group()
    pw.add_argument("--password-file", help="file holding the camera's management password")
    pw.add_argument("--password", help=argparse.SUPPRESS)  # legacy: visible in `ps`; prefer the above
    # Otherwise the password comes from OSPREY_CAMERA_PASSWORD, or a prompt.
    configured = _configured()
    parser.add_argument("--host", default=os.environ.get("OSPREY_HOST") or configured.get("host"),
                        help="the address the camera should dial us on (default: OSPREY_HOST, "
                             "or host in the config file)")
    parser.add_argument("--control-port", type=int,
                        default=configured.get("ports", {}).get("control", CONTROL_PORT))
    parser.add_argument("--camera-port", type=int, default=CAMERA_HTTPS_PORT)
    parser.add_argument("--token", default="osprey", help="the adoption code, ours to choose")
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("--host is required: Osprey's address as the camera reaches it")

    logging.basicConfig(level=logging.INFO, format="  %(message)s", stream=sys.stderr)
    if args.password:
        log.warning("--password is visible in `ps` and shell history; use --password-file or OSPREY_CAMERA_PASSWORD")
        password = args.password
    elif args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").strip()
    else:
        password = os.environ.get("OSPREY_CAMERA_PASSWORD") or getpass.getpass("Camera management password: ")
    try:
        take_custody(
            args.camera, args.user, password, args.host, args.token,
            args.control_port, args.camera_port,
        )
    except ManageError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
