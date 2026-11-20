"""Manual wins, then resume: PTZ arbitration between people and autotrackers."""

from __future__ import annotations

import arbitration
import ptz
from arbitration import AUTO, MANUAL, PATROL, Arbiter
from model import Position
from test_adoption_flow import _ptz_harness

MAC = "AABBCCDDEEFF"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_manual_holds_autotracking_back_then_it_resumes() -> None:
    clock = Clock()
    arbiter = Arbiter(30, clock=clock)
    assert arbiter.admit(MAC, AUTO) == (True, False)
    assert arbiter.admit(MAC, MANUAL) == (True, True)  # replaces the auto move
    clock.now += 10
    assert arbiter.admit(MAC, AUTO) == (False, False)
    assert arbiter.admit(MAC, MANUAL) == (True, False)  # extends the hold
    clock.now += 29
    assert arbiter.admit(MAC, AUTO)[0] is False
    assert 0 < arbiter.remaining(MAC) <= 1
    clock.now += 2
    assert arbiter.admit(MAC, AUTO) == (True, False)
    assert arbiter.dropped == 2
    assert arbiter.remaining("OTHERCAMERA") == 0


def test_untagged_and_disabled_arbitration_never_block() -> None:
    arbiter = Arbiter(30)
    arbiter.admit(MAC, MANUAL)
    assert arbiter.admit(MAC, None) == (True, False)
    off = Arbiter(0)
    off.admit(MAC, MANUAL)
    assert off.admit(MAC, AUTO) == (True, False)


def test_an_autotracker_takes_the_camera_from_a_patrol() -> None:
    """A tracked object outranks idle-time patrol work."""
    clock = Clock()
    arbiter = Arbiter(30, clock=clock)
    assert arbiter.admit(MAC, PATROL) == (True, False)
    # The autotracker may replace the patrol move that is still in flight.
    assert arbiter.admit(MAC, AUTO) == (True, True)
    # A patrol never replaces an autotracker move.
    assert arbiter.admit(MAC, PATROL) == (True, False)


def test_the_autotracker_takes_the_camera_from_a_patrol_with_arbitration_off() -> None:
    """Turning the manual hold off must not hand the camera to the patrol."""
    off = Arbiter(0)
    assert off.admit(MAC, PATROL) == (True, False)
    assert off.admit(MAC, AUTO) == (True, True)


def _as(kind: str, call):  # type: ignore[no-untyped-def]
    token = arbitration.source.set(kind)
    try:
        return call()
    finally:
        arbitration.source.reset(token)


def test_controller_lets_a_person_take_over_an_autotracker_move() -> None:
    ctl, session, wire = _ptz_harness()
    assert _as(AUTO, lambda: ctl.move(MAC, Position(pan=1000, tilt=9000)))
    assert session.camera.motion.status == "MOVING"
    # Without arbitration this second move is refused as "already moving".
    assert _as(MANUAL, lambda: ctl.move(MAC, Position(pan=2000, tilt=9000)))
    sent = len(wire.sent())
    # During the hold Frigate's commands report success but send nothing.
    assert _as(AUTO, lambda: ctl.move(MAC, Position(pan=3000)))
    assert _as(AUTO, lambda: ctl.stop_motion(MAC))
    assert _as(AUTO, lambda: ctl.goto_preset(MAC, 1))
    assert len(wire.sent()) == sent
    assert [m.payload["action"] for m in wire.sent() if m.function_name == ptz.PRESET][-1] == "go"


def test_a_patrol_move_does_not_lock_out_the_autotracker() -> None:
    """The patrol must not keep the camera from a tracked object.

    A patrol move keeps the head travelling for a while.  Every autotracking
    command that arrives in that window used to be refused as "the camera is
    already moving", so the tracking never started.
    """
    ctl, session, wire = _ptz_harness()
    assert _as(PATROL, lambda: ctl.move(MAC, Position(pan=1000, tilt=9000)))
    assert session.camera.motion.status == "MOVING"
    assert _as(AUTO, lambda: ctl.move(MAC, Position(pan=2000, tilt=9000)))
    assert session.camera.motion.status == "MOVING"
    actions = [m.payload["action"] for m in wire.sent() if m.function_name == ptz.PRESET]
    assert actions == ["config", "go", "config", "go"]


def test_a_patrol_never_displaces_an_autotracker_move() -> None:
    """The patrol stands down rather than fighting for the camera."""
    ctl, session, _ = _ptz_harness()
    assert _as(AUTO, lambda: ctl.move(MAC, Position(pan=1000, tilt=9000)))
    assert session.camera.motion.status == "MOVING"
    assert not _as(PATROL, lambda: ctl.move(MAC, Position(pan=2000, tilt=9000)))


def test_onvif_classifies_callers() -> None:
    import onvif
    from principals import Principal

    services = onvif.Services(onvif.Backend(camera=lambda: None, stream_uri=str, snapshot_uri=str),
                              host="127.0.0.1", autotrack_users=frozenset({"frigate"}))
    def user(name: str) -> onvif.Caller:
        return onvif.Caller(principal=Principal(username=name, password="x", roles=frozenset({"ptz"})), anonymous=False)
    assert services.ptz_source(onvif.Caller()) == AUTO
    assert services.ptz_source(user("frigate")) == AUTO
    assert services.ptz_source(user("protect")) == MANUAL
