"""The head, and the channel that drives it. No controller, no network."""

from __future__ import annotations

from typing import Any

import pytest

import gimbal as gimbal_module
import identity
import ptzchannel
from unifiwire import wsclient
from unifiwire.envelope import CAMERA, CONTROLLER, Envelope, decode


def a_channel() -> ptzchannel.PtzChannel:
    return ptzchannel.PtzChannel(
        uri="wss://10.0.0.1/camera/1.0/ws", who=identity.Identity(), certificate=None
    )


def from_controller(name: str, payload: dict[str, Any], mid: int = 5) -> Envelope:
    return Envelope(
        function_name=name, payload=dict(payload), message_id=mid,
        sender=CONTROLLER, recipient=CAMERA,
    )


def preset_item(index: int = 1, **axes: int) -> dict[str, Any]:
    item: dict[str, Any] = {"index": index, "name": "somewhere"}
    item.update(axes)
    return item


# ------------------------------------------------------------------------- head


def test_the_head_starts_parked_at_its_own_limits() -> None:
    head = gimbal_module.Gimbal()
    assert head.position()["pan"] == identity.PAN_STEPS[0]
    assert not head.moving


def test_going_to_an_unknown_preset_is_refused_not_guessed() -> None:
    assert not gimbal_module.Gimbal().go(7)


def test_a_configured_preset_can_be_travelled_to() -> None:
    head = gimbal_module.Gimbal()
    head.configure([preset_item(1, pan=20000, tilt=12000, zoom=100, focus=60)])
    assert head.go(1)
    assert head.moving


def test_travel_takes_time_and_ends_exactly_on_the_target() -> None:
    """The last broadcast has to match the request exactly — that is arrival."""
    head = gimbal_module.Gimbal()
    head.configure([preset_item(1, pan=20000, tilt=12000)])
    head.go(1, speed=1000)
    now = head._last_step
    assert head.step(now + 0.05), "one short step should not finish a long sweep"
    assert head.position()["pan"] < 20000
    for tick in range(2, 400):
        if not head.step(now + 0.05 * tick):
            break
    assert not head.moving
    assert head.position()["pan"] == 20000 and head.position()["tilt"] == 12000


def test_speed_changes_how_far_one_step_travels() -> None:
    far, near = gimbal_module.Gimbal(), gimbal_module.Gimbal()
    for head, speed in ((far, 2000), (near, 200)):
        head.configure([preset_item(1, pan=30000)])
        head.go(1, speed=speed)
        head.step(head._last_step + 0.1)
    assert far.position()["pan"] > near.position()["pan"]


def test_the_head_will_not_travel_past_its_limits() -> None:
    head = gimbal_module.Gimbal()
    head.configure([preset_item(1, pan=999_999, tilt=-999)])
    head.go(1, speed=100_000)
    head.step(head._last_step + 5.0)
    assert head.position()["pan"] == identity.PAN_STEPS[1]
    assert head.position()["tilt"] == identity.TILT_STEPS[0]


def test_pan_tilt_and_zoom_advance_together_and_clamp() -> None:
    channel = a_channel()
    channel.handle(from_controller(
        ptzchannel.PRESET,
        {"action": "config", "items": [preset_item(1, pan=20000, tilt=12000, zoom=999_999)]},
    ))
    channel.handle(from_controller(
        ptzchannel.PRESET, {"action": "go", "index": 1, "speed": 1000}
    ))
    now = channel.head._last_step

    assert channel.head.step(now + 0.01)
    moving_event = channel.broadcast()
    assert moving_event.function_name == ptzchannel.EVENT_MOTOR_STATE
    moving = moving_event.payload["state"]
    assert moving["activity"] == gimbal_module.ACTIVITY_MOVING
    assert moving["position"]["pan"] > identity.PAN_STEPS[0]
    assert moving["position"]["tilt"] > identity.TILT_STEPS[0]
    assert moving["position"]["zoom"] > identity.ZOOM_STEPS[0]

    for tick in range(2, 400):
        if not channel.head.step(now + 0.05 * tick):
            break
    settled_event = channel.broadcast()
    assert settled_event.function_name == ptzchannel.EVENT_MOTOR_STATE
    settled = settled_event.payload["state"]
    assert settled["activity"] == gimbal_module.ACTIVITY_SETTLED
    assert settled["position"] == {
        "pan": 20000,
        "tilt": 12000,
        "zoom": identity.ZOOM_STEPS[1],
        "focus": 58,
    }


def test_a_zoom_only_preset_leaves_pan_and_tilt_unchanged() -> None:
    head = gimbal_module.Gimbal()
    before = head.position()
    head.configure([preset_item(1, zoom=400)])
    head.go(1, speed=1000)
    now = head._last_step

    assert head.step(now + 0.01)
    moving = head.motor_state()["state"]
    assert moving["activity"] == gimbal_module.ACTIVITY_MOVING
    assert moving["position"]["zoom"] > before["zoom"]
    assert moving["position"]["pan"] == before["pan"]
    assert moving["position"]["tilt"] == before["tilt"]

    head.step(now + 1.0)
    settled = head.motor_state()["state"]
    assert settled["activity"] == gimbal_module.ACTIVITY_SETTLED
    assert settled["position"]["zoom"] == 400
    assert settled["position"]["pan"] == before["pan"]
    assert settled["position"]["tilt"] == before["tilt"]


def test_motor_state_has_the_shape_the_controller_reads() -> None:
    head = gimbal_module.Gimbal()
    head.configure([preset_item(1, pan=20000)])
    head.go(1)
    payload = head.motor_state()
    assert payload["ignoreActivity"] is True
    state = payload["state"]
    assert state["activity"] == gimbal_module.ACTIVITY_MOVING
    assert state["scale"] == "normalized" and state["focusMode"] == "manual"
    assert set(state["position"]) == {"pan", "tilt", "zoom", "focus"}
    assert isinstance(state["wallClockMs"], int)


def test_activity_returns_to_zero_once_settled() -> None:
    """Zero is what tells the controller the head has stopped."""
    head = gimbal_module.Gimbal()
    head.configure([preset_item(1, pan=int(head.pan.position))])
    head.go(1)
    head.step(head._last_step + 1.0)
    assert head.motor_state()["state"]["activity"] == gimbal_module.ACTIVITY_SETTLED


def test_current_position_answers_both_unit_systems() -> None:
    head = gimbal_module.Gimbal()
    both = head.current_position(in_degree=True, in_steps=True)
    assert "position" in both and "degrees" in both
    steps_only = head.current_position(in_degree=False, in_steps=True)
    assert "degrees" not in steps_only


def test_degrees_track_the_announced_range() -> None:
    head = gimbal_module.Gimbal()
    head.pan.position = float(identity.PAN_STEPS[1])
    assert head.current_position()["degrees"]["pan"] == identity.PAN_DEGREES[1]
    head.pan.position = float(identity.PAN_STEPS[0])
    assert head.current_position()["degrees"]["pan"] == identity.PAN_DEGREES[0]


# ---------------------------------------------------------------------- channel


def test_config_then_go_is_how_a_move_arrives() -> None:
    """There is no absolute-move verb: write a preset, then go to it."""
    channel = a_channel()
    channel.handle(from_controller(
        ptzchannel.PRESET,
        {"action": "config", "items": [preset_item(99, pan=23502, tilt=8000, zoom=0, focus=59)]},
    ))
    assert channel.head.presets[99]["pan"] == 23502
    channel.handle(from_controller(
        ptzchannel.PRESET, {"action": "go", "index": 99, "speed": 1000, "notifyCommandStatus": {}}
    ))
    assert channel.head.moving and channel.head.target["pan"] == 23502.0


def test_every_command_is_acknowledged_by_id() -> None:
    channel = a_channel()
    for payload in ({"action": "config", "items": []}, {"action": "go", "index": 1}):
        out = channel.handle(from_controller(ptzchannel.PRESET, payload, mid=77))
        assert len(out) == 1 and out[0].in_response_to == 77
        assert out[0].function_name == ptzchannel.PRESET and out[0].sender == CAMERA


def test_go_to_a_preset_we_were_never_given_is_still_acknowledged() -> None:
    """Silence would leave the controller waiting; the head simply does not move."""
    channel = a_channel()
    out = channel.handle(from_controller(ptzchannel.PRESET, {"action": "go", "index": 4}))
    assert len(out) == 1 and not channel.head.moving


def test_position_is_polled_as_well_as_broadcast() -> None:
    channel = a_channel()
    out = channel.handle(
        from_controller(ptzchannel.GET_POSITION, {"inDegree": True, "inSteps": True}, mid=3)
    )
    assert len(out) == 1 and out[0].in_response_to == 3
    assert "position" in out[0].payload and "degrees" in out[0].payload


def test_broadcasts_are_events_not_replies() -> None:
    channel = a_channel()
    event = channel.broadcast()
    assert event.function_name == ptzchannel.EVENT_MOTOR_STATE
    assert event.in_response_to == 0 and event.sender == CAMERA
    assert channel.broadcasts == 1


def test_reconnect_closes_only_the_current_socket() -> None:
    class ClosingConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    channel = a_channel()
    connection = ClosingConnection()
    channel.connection = connection  # type: ignore[assignment]

    channel.reconnect()

    assert connection.closed
    assert not channel.stopping.is_set()


def test_reconnected_pump_immediately_announces_settled_head() -> None:
    channel = a_channel()
    sent: list[bytes] = []

    class OneCycleConnection:
        def send(self, payload: bytes) -> None:
            sent.append(payload)

        def receive(self, timeout: float | None = None) -> list[object]:
            channel.stopping.set()
            return []

    channel._pump(OneCycleConnection())  # type: ignore[arg-type]

    assert len(sent) == 1
    event = decode(sent[0])
    assert event.function_name == ptzchannel.EVENT_MOTOR_STATE
    assert event.payload["state"]["activity"] == gimbal_module.ACTIVITY_SETTLED
    assert event.payload["state"]["position"] == channel.head.position()


def test_an_unknown_ptz_verb_is_acknowledged_rather_than_ignored() -> None:
    channel = a_channel()
    out = channel.handle(from_controller("SomethingNew", {"a": 1}, mid=9))
    assert len(out) == 1 and out[0].in_response_to == 9


def test_our_own_broadcasts_are_not_treated_as_commands() -> None:
    channel = a_channel()
    assert channel.handle_bytes(channel.broadcast().to_json()) == []


def test_the_ptz_uri_is_parsed_for_host_and_port() -> None:
    channel = ptzchannel.PtzChannel(
        uri="wss://10.0.0.9:7443/camera/1.0/ws", who=identity.Identity(), certificate=None
    )
    assert channel._target() == ("10.0.0.9", 7443)
    default = a_channel()
    assert default._target() == ("10.0.0.1", 7442), "no port means the control port"


def test_autotrack_settings_are_echoed() -> None:
    channel = a_channel()
    out = channel.handle(from_controller(ptzchannel.CHANGE_AUTOTRACK, {"trackTimeoutSec": 20}))
    assert out[0].payload == {"trackTimeoutSec": 20}


def test_a_dropped_ptz_socket_is_reopened(monkeypatch: pytest.MonkeyPatch) -> None:
    """The management channel may stay up while the dedicated socket is reset."""
    channel = a_channel()
    connections: list[FakeConnection] = []

    class FakeConnection:
        def __init__(self, drop: bool) -> None:
            self.drop = drop
            self.closed = False

        def send(self, payload: bytes) -> None:
            pass

        def receive(self, timeout: float | None = None) -> list[object]:
            if self.drop:
                raise ConnectionError("test disconnect")
            channel.stopping.set()
            return []

        def close(self) -> None:
            self.closed = True

    def connect(**kwargs: Any) -> FakeConnection:
        connection = FakeConnection(drop=not connections)
        connections.append(connection)
        return connection

    monkeypatch.setattr(wsclient, "connect", connect)
    monkeypatch.setattr(ptzchannel, "RECONNECT_INITIAL_SEC", 0.001)

    channel._run()

    assert len(connections) == 2
    assert all(connection.closed for connection in connections)


def test_ptz_reconnect_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = a_channel()
    delays: list[float] = []

    def refused(**kwargs: Any) -> None:
        raise OSError("test refusal")

    def wait(delay: float) -> bool:
        delays.append(delay)
        return len(delays) == 4

    monkeypatch.setattr(wsclient, "connect", refused)
    monkeypatch.setattr(channel.stopping, "wait", wait)
    monkeypatch.setattr(ptzchannel, "RECONNECT_INITIAL_SEC", 1.0)
    monkeypatch.setattr(ptzchannel, "RECONNECT_MAX_SEC", 2.0)

    channel._run()

    assert delays == [1.0, 2.0, 2.0, 2.0]
