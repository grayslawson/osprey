"""The Frigate camera block and Add to Frigate, against a fake Frigate config API."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import frigate_setup
from frigate_setup import FrigateError
from onboarding import Onboarding, OnboardingError

BLOCK = frigate_setup.camera_block("g5_ptz", "192.0.2.10", 8554, 8000, ("osprey", "r-pw"), ("frigate", "o'pw"))


def test_the_block_fills_in_host_ports_and_credentials() -> None:
    assert BLOCK == (
        "  g5_ptz:\n"
        "    ffmpeg:\n"
        "      inputs:\n"
        "        - path: rtsp://osprey:r-pw@192.0.2.10:8554/video2\n"
        "          roles: [detect, record]\n"
        "    onvif:\n"
        "      host: 192.0.2.10\n"
        "      port: 8000\n"
        "      user: 'frigate'\n"
        "      password: 'o''pw'\n"
    )


def test_an_anonymous_install_gets_a_block_without_credentials() -> None:
    block = frigate_setup.camera_block("cam", "192.0.2.10", 8554, 8000, None, None)
    assert "rtsp://192.0.2.10:8554/video2" in block and "user:" not in block


def test_url_reserved_characters_in_an_rtsp_password_are_escaped() -> None:
    block = frigate_setup.camera_block("cam", "192.0.2.10", 8554, 8000, ("osprey", "a@b:c/d"), None)
    assert "rtsp://osprey:a%40b%3Ac%2Fd@192.0.2.10" in block


@pytest.mark.parametrize("name, expected", [("Driveway PTZ", "driveway_ptz"), ("  ", "g5_ptz"), ("G5-PTZ!", "g5-ptz")])
def test_camera_names_follow_frigate_rules(name: str, expected: str) -> None:
    assert frigate_setup.camera_name(name) == expected


EXISTING = """mqtt:
  enabled: false  # keep comments

cameras:
  # the garden
  garden:
    ffmpeg:
      inputs:
        - path: rtsp://garden/stream
          roles: [record]
record:
  enabled: true
"""


def test_the_camera_goes_first_under_cameras_and_nothing_else_moves() -> None:
    merged = frigate_setup.merge(EXISTING, "g5_ptz", BLOCK)
    assert merged == EXISTING.replace("cameras:\n", "cameras:\n" + BLOCK)


def test_a_camera_of_the_same_name_is_never_replaced() -> None:
    with pytest.raises(FrigateError, match="already has a camera named garden") as caught:
        frigate_setup.merge(EXISTING, "garden", BLOCK)
    assert caught.value.code == "conflict"


def test_a_config_without_cameras_gets_the_section() -> None:
    assert frigate_setup.merge("mqtt:\n  enabled: false", "g5_ptz", BLOCK) == (
        "mqtt:\n  enabled: false\ncameras:\n" + BLOCK
    )


def test_the_block_follows_the_config_s_own_indentation() -> None:
    four = "cameras:\n    garden:\n        ffmpeg: {}\n"
    merged = frigate_setup.merge(four, "g5_ptz", BLOCK)
    assert merged.startswith("cameras:\n    g5_ptz:\n      ffmpeg:\n")
    assert merged.endswith("    garden:\n        ffmpeg: {}\n")


@pytest.mark.parametrize("raw", ["cameras: {}\n", "cameras: !include cameras.yaml\n"])
def test_a_section_that_is_not_a_plain_block_is_left_alone(raw: str) -> None:
    with pytest.raises(FrigateError, match="by hand"):
        frigate_setup.merge(raw, "g5_ptz", BLOCK)


# --------------------------------------------------------------- fake Frigate


@dataclass
class FakeFrigate:
    config: str = EXISTING
    user: str = ""          # set to require a login, as Frigate's port 8971 does
    password: str = ""
    saved: list[tuple[str, str]] = field(default_factory=list)
    refuse_save: bool = False


@pytest.fixture
def frigate() -> Iterator[tuple[FakeFrigate, str]]:
    state = FakeFrigate()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def send(self, code: int, body: object, headers: dict[str, str] | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def allowed(self) -> bool:
            if not state.user or "frigate_token=jwt" in self.headers.get("Cookie", ""):
                return True
            self.send(401, {"message": "Unauthorized"})
            return False

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/config/raw" and self.allowed():
                self.send(200, state.config)  # Frigate sends the YAML as a JSON string

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/api/login":
                login = json.loads(body)
                if login == {"user": state.user, "password": state.password}:
                    self.send(200, "", {"Set-Cookie": "frigate_token=jwt; Path=/; HttpOnly"})
                else:
                    self.send(401, {"message": "Login failed"})
            elif self.path.startswith("/api/config/save?save_option=") and self.allowed():
                if state.refuse_save:
                    self.send(400, {"success": False, "message": "Your configuration is invalid."})
                    return
                assert self.headers.get("Content-Type") == "text/plain"
                state.saved.append((self.path.rsplit("=", 1)[1], body.decode()))
                state.config = body.decode()
                self.send(200, {"success": True, "message": "Config successfully saved, restarting"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def steps(tmp_path: Path) -> Onboarding:
    return Onboarding(
        host="192.0.2.10", control_port=7442, cameras=({"mac": "AABBCCDDEEFF", "name": "G5 PTZ"},),
        rtsp_login=("osprey", "r-pw"), onvif_login=("frigate", "o-pw"), state_dir=tmp_path,
    )


def test_preview_then_apply_adds_the_camera_with_a_backup(tmp_path: Path, frigate: tuple[FakeFrigate, str]) -> None:
    fake, url = frigate
    preview = steps(tmp_path).frigate_preview({}, url)
    assert preview["name"] == "g5_ptz"
    assert "+  g5_ptz:" in str(preview["diff"]) and "+      user: 'frigate'" in str(preview["diff"])
    assert fake.saved == []  # a preview changes nothing

    result = steps(tmp_path).frigate_apply({"base": preview["base"]}, url)
    assert fake.saved == [("restart", EXISTING.replace("cameras:\n", "cameras:\n" + str(steps(tmp_path).frigate_block({})["block"])))]
    backup = Path(str(result["backup"]))
    assert backup.read_text() == EXISTING and backup.stat().st_mode & 0o777 == 0o600


def test_apply_refuses_when_frigate_changed_since_the_preview(
    tmp_path: Path, frigate: tuple[FakeFrigate, str],
) -> None:
    fake, url = frigate
    preview = steps(tmp_path).frigate_preview({}, url)
    fake.config += "# edited meanwhile\n"
    with pytest.raises(OnboardingError, match="changed since the preview") as caught:
        steps(tmp_path).frigate_apply({"base": preview["base"]}, url)
    assert caught.value.code == "frigate_changed" and fake.saved == []


def test_a_name_frigate_already_has_is_a_conflict(tmp_path: Path, frigate: tuple[FakeFrigate, str]) -> None:
    _, url = frigate
    with pytest.raises(OnboardingError) as caught:
        steps(tmp_path).frigate_preview({"name": "garden"}, url)
    assert caught.value.status == 409 and caught.value.code == "frigate_conflict"


def test_a_frigate_with_a_login_asks_for_one_then_uses_it(
    tmp_path: Path, frigate: tuple[FakeFrigate, str],
) -> None:
    fake, url = frigate
    fake.user, fake.password = "admin", "frigate-pw"
    with pytest.raises(OnboardingError) as caught:
        steps(tmp_path).frigate_preview({}, url)
    assert caught.value.code == "frigate_auth"
    with pytest.raises(OnboardingError, match="refused the login"):
        steps(tmp_path).frigate_preview({"username": "admin", "password": "wrong"}, url)
    login = {"username": "admin", "password": "frigate-pw"}
    preview = steps(tmp_path).frigate_preview(login, url)
    steps(tmp_path).frigate_apply({**login, "base": preview["base"], "restart": False}, url)
    assert fake.saved[0][0] == "saveonly"


def test_frigate_refusing_the_config_is_reported_and_the_backup_kept(
    tmp_path: Path, frigate: tuple[FakeFrigate, str],
) -> None:
    fake, url = frigate
    fake.refuse_save = True
    preview = steps(tmp_path).frigate_preview({}, url)
    with pytest.raises(OnboardingError, match="configuration is invalid.*not changed"):
        steps(tmp_path).frigate_apply({"base": preview["base"]}, url)
    assert fake.config == EXISTING


def test_no_frigate_url_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(OnboardingError, match="Frigate URL"):
        steps(tmp_path).frigate_preview({}, None)
    with pytest.raises(OnboardingError, match="http or https"):
        steps(tmp_path).frigate_preview({"url": "ftp://frigate"}, None)


def test_an_unreachable_frigate_is_reported(tmp_path: Path) -> None:
    with pytest.raises(OnboardingError, match="cannot reach Frigate"):
        steps(tmp_path).frigate_preview({"url": "http://127.0.0.1:9"}, None)
