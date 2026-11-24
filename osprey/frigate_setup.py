"""The Frigate camera block, and adding it to Frigate through Frigate's config API.

Osprey does not install or run Frigate; this only edits the user's Frigate
config when they ask, the way Frigate's own config editor does:

    GET  {frigate}/api/config/raw                         the YAML, as a JSON string
    POST {frigate}/api/config/save?save_option=restart    the new YAML, text/plain

Frigate validates the YAML on save and refuses an invalid one. The merge is a
text insertion under the top-level ``cameras:`` key, so comments and layout
survive, and a camera of the same name is never replaced. A Frigate behind its
own login (port 8971) takes ``POST /api/login`` with a user and password; the
JWT cookie it sets is sent back on the config calls.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Final
from urllib.parse import quote

TIMEOUT_SEC: Final = 15.0
MAX_CONFIG_BYTES: Final = 1024 * 1024
_TOP_LEVEL_CAMERAS = re.compile(r"^cameras:[ \t]*(#.*)?$")
_CHILD = re.compile(r"^( +)([^\s#:][^:]*?)[ \t]*:(?:[ \t]|$)")


class FrigateError(ValueError):
    """``code`` tells the wizard what to ask for: ``auth``, ``conflict``, ``changed`` or ``failed``."""

    def __init__(self, message: str, code: str = "failed") -> None:
        super().__init__(message)
        self.code = code


def camera_name(name: str) -> str:
    """A Frigate camera name (letters, digits, ``_`` and ``-``) from a display name."""
    return re.sub(r"[^a-z0-9_-]+", "_", name.strip().lower()).strip("_")[:64] or "g5_ptz"


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def camera_block(name: str, host: str, rtsp_port: int, onvif_port: int,
                 rtsp: tuple[str, str] | None, onvif: tuple[str, str] | None,
                 track: str = "video2") -> str:
    """One entry for Frigate's ``cameras:`` map, indented two spaces."""
    address = f"[{host}]" if ":" in host else host
    login = f"{quote(rtsp[0], safe='')}:{quote(rtsp[1], safe='')}@" if rtsp else ""
    lines = [
        f"  {name}:",
        "    ffmpeg:",
        "      inputs:",
        f"        - path: rtsp://{login}{address}:{rtsp_port}/{track}",
        "          roles: [detect, record]",
        "    onvif:",
        f"      host: {host}",
        f"      port: {onvif_port}",
    ]
    if onvif:
        lines += [f"      user: {_quoted(onvif[0])}", f"      password: {_quoted(onvif[1])}"]
    return "\n".join(lines) + "\n"


def merge(raw: str, name: str, block: str) -> str:
    """``raw`` with ``block`` added as the first camera; never replaces one."""
    lines = raw.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next((i for i, line in enumerate(lines) if _TOP_LEVEL_CAMERAS.match(line.rstrip("\r\n"))), None)
    if start is None:
        if any(line.startswith("cameras:") for line in lines):
            raise FrigateError("Frigate's cameras: section is not a plain block; add the camera by hand")
        return "".join(lines) + "cameras:\n" + block
    indent = 0
    names: set[str] = set()
    for line in lines[start + 1:]:
        text = line.rstrip("\r\n")
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        if not text.startswith((" ", "\t")):
            break  # the next top-level key
        match = _CHILD.match(text)
        if match and (indent == 0 or len(match.group(1)) == indent):
            indent = len(match.group(1))
            names.add(match.group(2).strip("'\""))
    if name in names:
        raise FrigateError(f"Frigate already has a camera named {name}. Choose another name.", "conflict")
    if indent and indent != 2:
        block = "".join(" " * (indent - 2) + line for line in block.splitlines(keepends=True))
    return "".join(lines[: start + 1]) + block + "".join(lines[start + 1:])


def diff(before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        "Frigate config now", "with the Osprey camera",
    ))


def fingerprint(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class Frigate:
    url: str
    user: str = ""
    password: str = ""
    insecure_tls: bool = False

    def __post_init__(self) -> None:
        context = ssl.create_default_context()
        if self.insecure_tls:  # Frigate's port 8971 ships a self-signed certificate
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(CookieJar()),
        )
        self._signed_in = False

    def _call(self, method: str, path: str, body: bytes | None = None,
              content_type: str = "application/json") -> bytes:
        request = urllib.request.Request(self.url + path, data=body, method=method,
                                         headers={"Content-Type": content_type} if body is not None else {})
        try:
            with self._opener.open(request, timeout=TIMEOUT_SEC) as response:
                data: bytes = response.read(MAX_CONFIG_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not path.startswith("/api/login"):
                if self.user and not self._signed_in:
                    self._login()
                    return self._call(method, path, body, content_type)
                raise FrigateError("Frigate asks for a login. Enter a Frigate admin user and password.",
                                   "auth") from None
            detail = _message(exc.read(4096))
            raise FrigateError(f"Frigate answered {exc.code}{': ' + detail if detail else ''}") from None
        except ssl.SSLCertVerificationError:
            raise FrigateError("Frigate's TLS certificate is not trusted. If it is Frigate's own "
                               "self-signed certificate, allow it and try again.", "tls") from None
        except OSError as exc:
            raise FrigateError(f"cannot reach Frigate at {self.url}: {exc}") from None
        if len(data) > MAX_CONFIG_BYTES:
            raise FrigateError("Frigate's reply is too large")
        return data

    def _login(self) -> None:
        self._signed_in = True
        try:
            self._call("POST", "/api/login", json.dumps({"user": self.user, "password": self.password}).encode())
        except FrigateError as exc:
            raise FrigateError(f"Frigate refused the login ({exc}).", "auth") from None

    def raw_config(self) -> str:
        data = self._call("GET", "/api/config/raw")
        try:
            value = json.loads(data)
        except ValueError:
            value = data.decode("utf-8", errors="replace")  # an older Frigate sends plain text
        if not isinstance(value, str):
            raise FrigateError("Frigate did not return its config file")
        return value

    def save(self, text: str, restart: bool) -> str:
        option = "restart" if restart else "saveonly"
        data = self._call("POST", f"/api/config/save?save_option={option}", text.encode(), "text/plain")
        return _message(data) or "Frigate saved the config."


def _message(data: bytes) -> str:
    try:
        value = json.loads(data)
    except ValueError:
        return data.decode("utf-8", errors="replace").strip()[:300]
    return str(value.get("message", ""))[:600] if isinstance(value, dict) else ""


def backup(state_dir: Path, raw: str) -> Path:
    """Keep Frigate's config as it was, mode 0600: it can hold Frigate's own secrets."""
    path = state_dir / time.strftime("frigate-config-backup-%Y%m%d-%H%M%S.yaml")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(raw)
    return path
