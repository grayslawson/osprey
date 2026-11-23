"""Startup option resolution: error paths and environment overrides."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import main
from test_config import _args


def _config(tmp_path: Path, **values: object) -> str:
    path = tmp_path / "osprey.json"
    path.write_text(json.dumps({"host": "10.0.0.5", **values}))
    return str(path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OSPREY_BIND", "OSPREY_PTZ_MAX_SPEED", "OSPREY_CONFIG_FILE", "OSPREY_SETUP", "OSPREY_HOST"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("values, message", [
    ({"ports": {"onvif": 70000}}, "invalid runtime configuration"),
    ({"cameras": [{"mac": "nope"}]}, "invalid camera registry"),
    ({"cameras": "x"}, "invalid"),
    ({"ptz": {"max_speed": "fast"}}, "invalid runtime configuration"),
    ({"frigate": {"url": 5}}, "frigate.url"),
    ({"frigate": {"url": "ftp://x"}}, "invalid runtime configuration"),
    ({"mqtt": {"port": "x"}}, "invalid runtime configuration"),
    ({"discovery": {"hello_interval": "soon"}}, "invalid runtime configuration"),
    ({"onvif": {"identity": {"mac": "zz"}}}, "invalid runtime configuration"),
])
def test_bad_sections_exit_with_a_clear_message(tmp_path: Path, values: dict[str, object], message: str) -> None:
    with pytest.raises(SystemExit, match=message):
        main.resolve_options(_args(config=_config(tmp_path, **values)))


def test_unparseable_config_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "osprey.json"
    path.write_text("{not json")
    with pytest.raises(SystemExit, match="invalid config file"):
        main.resolve_options(_args(config=str(path)))


def test_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSPREY_BIND", "127.0.0.1")
    monkeypatch.setenv("OSPREY_PTZ_MAX_SPEED", "321")
    options = main.resolve_options(_args(config=_config(tmp_path)))
    assert options.bind_host == "127.0.0.1" and options.ptz_max_speed == 321


def test_non_integer_ptz_speed_env_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSPREY_PTZ_MAX_SPEED", "fast")
    with pytest.raises(SystemExit, match="OSPREY_PTZ_MAX_SPEED must be an integer"):
        main.resolve_options(_args(config=_config(tmp_path)))


def test_flags_override_ports_and_camera_filters(tmp_path: Path) -> None:
    options = main.resolve_options(_args(
        config=_config(tmp_path), onvif_port=9001, rtsp_port=9002, no_announce=True,
        expected_camera_ip="192.0.2.9", expected_camera_mac="aa-bb-cc-dd-ee-ff",
    ))
    assert (options.onvif_port, options.rtsp_port, options.announce) == (9001, 9002, False)
    assert options.expected_camera_ip == "192.0.2.9" and options.expected_camera_mac == "AABBCCDDEEFF"


def test_spec_to_tracks_and_unknown_codec() -> None:
    assert main.spec_to_tracks("video1, video2:h265,") == {"video1": "h264", "video2": "h265"}
    names, codecs = main.tracks_to_model({"video1": "vp9"})
    assert names == ("video1",) and codecs["video1"].value == "h264"


def test_preset_path_prefers_env_then_config_then_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    options = main.resolve_options(_args(config=_config(tmp_path)))
    assert main.preset_path(options) == options.cert.parent / "osprey-presets.json"
    monkeypatch.setenv("OSPREY_PRESETS_FILE", str(tmp_path / "p.json"))
    assert main.preset_path(options) == tmp_path / "p.json"


def test_rate_limited_warning_suppresses_repeats(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    warn = main.RateLimitedWarning(logging.getLogger("t"), interval=3600)
    with caplog.at_level(logging.WARNING, logger="t"):
        warn("camera %s is late", "a")
        warn("camera %s is late", "a")
        warn("camera %s is late", "b")
    assert [r.getMessage() for r in caplog.records] == ["camera a is late", "camera b is late"]
