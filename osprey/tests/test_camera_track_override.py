"""A camera may override the track codecs of the top-level ``tracks`` map.

``cameras[].tracks`` was accepted and documented but never read: the map a
camera carried was dropped on the floor and every camera got the top-level
codecs. These tests pin the behaviour of the override.
"""

from pathlib import Path

import adoption
import controller
from model import Camera, Codec


MAC = "AABBCCDDEEFF"


def _controller(
    configured: dict[str, dict[str, object]] | None = None,
    codecs: dict[str, Codec] | None = None,
) -> controller.Controller:
    ctl = controller.Controller(
        cert=Path("unused.pem"),
        ingest_host="10.0.0.1",
        track_codecs=codecs if codecs is not None else {"video1": Codec.H264, "video2": Codec.H264},
        configured_cameras=configured,
    )
    return ctl


def test_a_camera_without_an_override_gets_the_top_level_codecs() -> None:
    ctl = _controller()
    assert ctl._codecs_for(MAC) == {"video1": Codec.H264, "video2": Codec.H264}


def test_a_camera_override_wins_for_the_track_it_names() -> None:
    ctl = _controller(
        configured={MAC: {"mac": MAC, "name": "ptz", "tracks": {"video2": Codec.H265.value}}},
        codecs={"video1": Codec.H264, "video2": Codec.H264},
    )
    codecs = ctl._codecs_for(MAC)
    assert codecs["video2"] == Codec.H265
    # The tracks the override does not name keep the top-level codec.
    assert codecs["video1"] == Codec.H264


def test_a_camera_override_may_name_a_track_the_top_level_does_not() -> None:
    ctl = _controller(
        configured={MAC: {"mac": MAC, "name": "ptz", "tracks": {"video3": Codec.H265.value}}},
        codecs={"video1": Codec.H264},
    )
    codecs = ctl._codecs_for(MAC)
    assert codecs == {"video1": Codec.H264, "video3": Codec.H265}


def test_an_unknown_codec_falls_back_to_h264() -> None:
    ctl = _controller(
        configured={MAC: {"mac": MAC, "name": "ptz", "tracks": {"video2": "vp9"}}},
    )
    assert ctl._codecs_for(MAC)["video2"] == Codec.H264


def test_an_empty_override_changes_nothing() -> None:
    ctl = _controller(configured={MAC: {"mac": MAC, "name": "ptz", "tracks": {}}})
    assert ctl._codecs_for(MAC) == {"video1": Codec.H264, "video2": Codec.H264}


def test_the_override_reaches_the_camera_tracks() -> None:
    """The map is not only computed: it is what the camera is pinned to."""
    ctl = _controller(
        configured={MAC: {"mac": MAC, "name": "ptz", "tracks": {"video2": Codec.H265.value}}},
        codecs={"video1": Codec.H264, "video2": Codec.H264},
    )
    camera = Camera(mac=MAC)
    camera.tracks = adoption.track_defaults()
    adoption.set_track_codecs(camera, ctl._codecs_for(MAC))
    assert {t.name: t.codec for t in camera.tracks}["video2"] == Codec.H265
    assert {t.name: t.codec for t in camera.tracks}["video1"] == Codec.H264
