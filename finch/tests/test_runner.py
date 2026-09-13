"""Runner lifecycle tests that do not open sockets or touch a controller."""

from __future__ import annotations

from pathlib import Path

import pytest

import camera
import identity
import main
import ptzchannel
import push
import virtual_lens


def test_repeated_ptz_callbacks_keep_the_same_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """A callback refresh replaces the socket, not the simulated camera position."""
    monkeypatch.setattr(ptzchannel.PtzChannel, "start", lambda self: None)
    monkeypatch.setattr(ptzchannel.PtzChannel, "stop", lambda self: None)
    who = identity.Identity()
    runner = main.Runner(
        options=main.Options(host="controller"),
        who=who,
        camera=camera.Camera(who=who),
    )
    runner.head.pan.position = 12345
    runner.head.tilt.position = 10000
    runner.head.zoom.position = 321

    runner.open_ptz("wss://controller/camera/1.0/ws")
    first = runner.ptz
    runner.open_ptz("wss://controller/camera/1.0/ws")

    assert first is not None and runner.ptz is not None and runner.ptz is not first
    assert runner.ptz.head is runner.head
    assert runner.ptz.head.position() == {
        "pan": 12345,
        "tilt": 10000,
        "zoom": 321,
        "focus": 58,
    }


def test_virtual_lens_stream_uses_live_head_without_double_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(push.Pusher, "start", lambda self: None)
    who = identity.Identity()
    state = Path("lens-state.json")
    runner = main.Runner(
        options=main.Options(
            host="controller",
            source=Path("local-g5.mp4"),
            fps=15,
            virtual_lens=True,
            lens_state=state,
        ),
        who=who,
        camera=camera.Camera(who=who),
    )

    runner.start_stream(camera.Stream("video2", "tcp://controller:7550", "video2"))

    pusher = runner.pushers["video2"]
    assert isinstance(pusher.source, virtual_lens.LensSource)
    assert pusher.source.head is runner.head
    assert pusher.source.state_path == state
    assert not pusher.pace
