"""Layer tests. No camera, no network, no controller."""

from __future__ import annotations

import json
from typing import Any

import pytest

import adoption
from unifiwire import envelope
from unifiwire import flv
import ptz
from unifiwire import ws
from model import AxisRange, Camera, Codec, Position, Preset, VideoTrack

# --------------------------------------------------------------------------- model


def test_axis_maps_both_ways() -> None:
    axis = AxisRange(500, 35500)
    assert axis.to_normalised(500) == pytest.approx(-1.0)
    assert axis.to_normalised(35500) == pytest.approx(1.0)
    assert axis.from_normalised(-1.0) == 500
    assert axis.from_normalised(1.0) == 35500
    midpoint = axis.from_normalised(0.0)
    assert axis.to_normalised(midpoint) == pytest.approx(0.0, abs=1e-4)


def test_axis_inversion_flips_sense() -> None:
    plain = AxisRange(0, 100)
    flipped = AxisRange(0, 100, invert=True)
    assert plain.to_normalised(100) == pytest.approx(1.0)
    assert flipped.to_normalised(100) == pytest.approx(-1.0)
    assert flipped.from_normalised(-1.0) == 100


def test_axis_clamps_out_of_range() -> None:
    axis = AxisRange(10, 20)
    assert axis.clamp(-5) == 10
    assert axis.clamp(99) == 20
    assert axis.from_normalised(-99.0) == 10


def test_degenerate_axis_is_not_ptz() -> None:
    camera = Camera(mac="AA")
    assert not camera.is_ptz
    assert camera.pan_range.to_normalised(5) == 0.0


def test_motion_settles_on_zero_activity() -> None:
    camera = Camera(mac="AA")
    camera.motion.connect()
    camera.motion.update(Position(pan=1), activity=16)
    assert not camera.motion.settled
    camera.motion.update(Position(pan=1), activity=0)
    assert camera.motion.settled


def test_motion_reports_unknown_when_the_ptz_channel_is_unavailable() -> None:
    camera = Camera(mac="AA")
    assert camera.motion.status == "UNKNOWN"


def test_reconnect_discards_stale_move_until_fresh_motor_state() -> None:
    camera = Camera(mac="AA")
    camera.motion.connect()
    assert camera.motion.begin(Position(pan=2))

    camera.motion.reconnect()
    assert camera.motion.status == "UNKNOWN"
    assert camera.motion.snapshot()[3] == "awaiting PTZ state"

    camera.motion.update(Position(pan=1), activity=0)
    assert camera.motion.status == "IDLE"
    assert camera.motion.position.pan == 1


def test_first_channel_is_idle_but_a_replacement_awaits_state() -> None:
    camera = Camera(mac="AA")
    assert not camera.motion.open_channel()
    assert camera.motion.status == "IDLE"

    assert camera.motion.open_channel()
    assert camera.motion.status == "UNKNOWN"
    camera.motion.connect()
    assert camera.motion.status == "IDLE"
    camera.motion.disconnect()
    assert camera.motion.status == "UNKNOWN"


def test_stale_idle_update_does_not_settle_a_reserved_move() -> None:
    camera = Camera(mac="AA")
    camera.motion.connect()
    initial = Position(pan=1)
    target = Position(pan=2)
    camera.motion.update(initial, activity=0)

    assert camera.motion.begin(target)
    camera.motion.update(initial, activity=0)
    assert camera.motion.status == "MOVING"
    camera.motion.update(target, activity=0)
    assert camera.motion.status == "IDLE"


def test_motion_rejects_overlapping_commands() -> None:
    camera = Camera(mac="AA")
    camera.motion.connect()

    assert camera.motion.begin(Position(pan=1))
    assert not camera.motion.begin(Position(pan=2))
    camera.motion.cancel()
    assert camera.motion.status == "IDLE"


def test_motion_becomes_unknown_when_terminal_events_stop() -> None:
    camera = Camera(mac="AA")
    camera.motion.connect()

    assert camera.motion.begin(Position(pan=1), timeout=0.0)
    assert camera.motion.status == "UNKNOWN"
    assert camera.motion.settled, "a timed-out reservation must not stay moving forever"
    assert camera.motion.begin(Position(pan=2))
    assert camera.motion.status == "MOVING"
    camera.motion.update(Position(pan=2), activity=0)
    assert camera.motion.status == "IDLE"


# ---------------------------------------------------------------------------- ptz


def _ptz_camera() -> Camera:
    camera = Camera(mac="AA")
    camera.pan_range = AxisRange(500, 35500)
    camera.tilt_range = AxisRange(8000, 18000)
    camera.zoom_range = AxisRange(0, 730)
    return camera


def test_move_is_configure_then_go() -> None:
    messages = ptz.move_to(Position(pan=23502, tilt=8000, zoom=0, focus=59))
    assert [m["action"] for m in messages] == ["config", "go"]
    item = messages[0]["items"][0]
    assert item["pan"] == 23502
    assert item["focus"] == 59
    assert messages[1]["index"] == item["index"]
    assert messages[1]["speed"] == ptz.DEFAULT_SPEED
    assert messages[1]["notifyCommandStatus"] == {}


def test_configure_carries_four_axes() -> None:
    payload = ptz.configure(Preset(3, "corner", Position(1, 2, 3, 4)))
    item = payload["items"][0]
    assert (item["pan"], item["tilt"], item["zoom"], item["focus"]) == (1, 2, 3, 4)


def test_get_position_requests_both_unit_systems() -> None:
    assert ptz.get_position() == {"inDegree": True, "inSteps": True}


def test_enable_carries_our_own_url() -> None:
    assert ptz.enable("wss://host/camera/1.0/ws")["uri"].endswith("/camera/1.0/ws")


def test_parse_motor_state() -> None:
    payload: dict[str, Any] = {
        "ignoreActivity": True,
        "state": {
            "activity": 16,
            "focusMode": "manual",
            "scale": "normalized",
            "position": {"focus": 58, "pan": 23502, "tilt": 8000, "zoom": 0},
            "wallClockMs": 1784989188609,
        },
    }
    parsed = ptz.parse_motor_state(payload)
    assert parsed is not None
    position, activity = parsed
    assert position.pan == 23502
    assert position.focus == 58
    assert activity == 16


def test_parse_motor_state_accepts_flat_position_reply() -> None:
    parsed = ptz.parse_motor_state(
        {
            "activity": 0,
            "position": {"focus": 0, "pan": 19767, "tilt": 14060, "zoom": 215},
        }
    )
    assert parsed == (Position(pan=19767, tilt=14060, zoom=215, focus=0), 0)


def test_parse_motor_state_accepts_g5_steps_reply() -> None:
    """The real G5 answers GetCurrentPosition with degree + steps, not position.

    Captured from a UVC G5 PTZ. Without this shape being understood every reply
    was discarded, so a reserved move could only settle on the 60 s timeout.
    """
    parsed = ptz.parse_motor_state(
        {
            "degree": {"pan": -175.0, "tilt": -10.0, "zoom": 1.0},
            "steps": {"focus": 46, "pan": 500, "tilt": 8000, "zoom": 0},
        }
    )
    assert parsed == (Position(pan=500, tilt=8000, zoom=0, focus=46), 0)


def test_focus_drift_does_not_hold_a_reserved_move() -> None:
    """The lens hunting must not keep a finished pan/tilt/zoom move reserved."""
    camera = _ptz_camera()
    camera.motion.connect()
    target = Position(pan=681, tilt=8000, zoom=0, focus=46)
    assert camera.motion.begin(target, timeout=60.0)
    assert camera.motion.status == "MOVING"
    # The gimbal arrived; only autofocus is still moving.
    camera.motion.update(Position(pan=681, tilt=8000, zoom=0, focus=58), activity=0)
    assert camera.motion.status == "IDLE"
    assert camera.motion.settled


def test_g5_encoder_rounding_settles_a_reserved_move() -> None:
    """The physical G5 can stop a few raw steps either side of its target."""
    camera = _ptz_camera()
    camera.motion.connect()
    target = Position(pan=19631, tilt=12335, zoom=668, focus=117)
    assert camera.motion.begin(target, timeout=60.0)
    # Captured physical result: pan +4, tilt -1, exact zoom, changed autofocus.
    camera.motion.update(
        Position(pan=19635, tilt=12334, zoom=668, focus=139),
        activity=16,
        confirmed=True,
    )
    assert camera.motion.status == "IDLE"
    assert camera.motion.settled


def test_g5_post_settle_activity_does_not_reopen_the_reservation() -> None:
    camera = _ptz_camera()
    camera.motion.connect()
    target = Position(pan=19631, tilt=12335, zoom=668, focus=117)
    assert camera.motion.begin(target)
    camera.motion.update(
        Position(pan=19635, tilt=12334, zoom=668, focus=139),
        activity=16,
        confirmed=True,
    )
    # Autofocus/motor events near the confirmed pose are not a new move.
    camera.motion.update(Position(pan=19635, tilt=12334, zoom=666, focus=112), activity=16)
    assert camera.motion.status == "IDLE"
    assert camera.motion.begin(Position(pan=19600, tilt=12335, zoom=668, focus=112))


def test_g5_ignored_activity_at_the_new_target_settles_the_new_move() -> None:
    """A delayed G5 motor frame can be the first terminal state of the next move.

    Frigate calibration sends adjacent zoom increments.  The G5 marks its
    terminal frame activity=16 but explicitly says to ignore that activity;
    once its pose matches the new target, the following increment must not be
    refused as busy.
    """
    camera = _ptz_camera()
    camera.motion.connect()
    target = Position(pan=19635, tilt=12334, zoom=730, focus=109)
    assert camera.motion.begin(target)
    camera.motion.update(
        Position(pan=19635, tilt=12334, zoom=730, focus=115),
        activity=16,
        ignore_activity=True,
    )
    assert camera.motion.status == "IDLE"
    assert camera.motion.begin(Position(pan=19635, tilt=12334, zoom=723, focus=115))


def test_unflagged_activity_at_a_target_does_not_settle_a_reserved_move() -> None:
    """Keep genuine motor motion from releasing a move prematurely."""
    camera = _ptz_camera()
    camera.motion.connect()
    target = Position(pan=19635, tilt=12334, zoom=730, focus=109)
    assert camera.motion.begin(target)
    camera.motion.update(
        Position(pan=19635, tilt=12334, zoom=730, focus=115), activity=16
    )
    assert camera.motion.status == "MOVING"
    assert not camera.motion.begin(Position(pan=19635, tilt=12334, zoom=723, focus=115))


def test_consecutive_moves_settle_from_position_replies() -> None:
    """Back-to-back moves must release the slot from a reply, not a timeout.

    Calibration and autotracking both issue moves continuously; the real G5
    answers each GetCurrentPosition with its new position.
    """
    camera = _ptz_camera()
    camera.motion.connect()
    camera.motion.update(Position(pan=500, tilt=8000, zoom=0, focus=46), activity=0)

    assert camera.motion.begin(Position(pan=681, tilt=8000, zoom=0, focus=46), timeout=60.0)
    assert camera.motion.status == "MOVING"
    camera.motion.update(Position(pan=681, tilt=8000, zoom=0, focus=58), activity=0)
    assert camera.motion.status == "IDLE"

    assert camera.motion.begin(
        Position(pan=862, tilt=8000, zoom=0, focus=58), timeout=60.0
    ), "the previous move must release the slot without hitting the timeout"
    camera.motion.update(Position(pan=862, tilt=8000, zoom=0, focus=41), activity=0)
    assert camera.motion.status == "IDLE"
    assert camera.motion.settled


@pytest.mark.parametrize("payload", [{}, {"state": 1}, {"state": {}}])
def test_parse_motor_state_rejects_malformed(payload: dict[str, Any]) -> None:
    assert ptz.parse_motor_state(payload) is None


def test_ranges_read_from_hello() -> None:
    features = {
        "pan": {"steps": {"min": 500, "max": 35500}},
        "tilt": {"steps": {"min": 8000, "max": 18000}},
        "zoom": {"steps": {"min": 0, "max": 730}},
    }
    pan, tilt, zoom = ptz.ranges_from_hello(features)
    assert (pan.minimum, pan.maximum) == (500, 35500)
    assert (tilt.minimum, tilt.maximum) == (8000, 18000)
    assert (zoom.minimum, zoom.maximum) == (0, 730)


def test_ranges_absent_are_degenerate() -> None:
    pan, _, _ = ptz.ranges_from_hello({})
    assert pan.minimum == pan.maximum == 0


def test_normalised_round_trip_preserves_focus() -> None:
    camera = _ptz_camera()
    camera.motion.update(Position(focus=42), activity=0)
    position = ptz.normalised_to_position(camera, 0.0, 1.0, -1.0)
    assert position.focus == 42
    assert position.tilt == 18000
    assert position.zoom == 0
    pan, tilt, zoom = ptz.position_to_normalised(camera, position)
    assert tilt == pytest.approx(1.0)
    assert zoom == pytest.approx(-1.0)


# ----------------------------------------------------------------------- adoption


def test_hello_reply_omits_adoption_code_and_features() -> None:
    payload = adoption.hello_reply()
    assert "adoptionCode" not in payload
    assert "features" not in payload
    assert payload["protocolVersion"] == adoption.PROTOCOL_VERSION


def test_hello_reply_overrides_any_held_identity() -> None:
    """null uuid plus overrideUuid true is what lets us take over an adopted camera."""
    payload = adoption.hello_reply()
    assert payload["controllerUuid"] is None
    assert payload["overrideUuid"] is True


def test_param_agreement_shape() -> None:
    assert adoption.param_agreement() == {
        "enableStatusCodes": True,
        "useHeartbeats": False,
        "heartbeatsTimeoutMs": adoption.HEARTBEAT_TIMEOUT_MS,
    }
    assert "authToken" not in adoption.param_agreement()


def test_time_sync_is_two_timestamps() -> None:
    assert adoption.time_sync_reply(1234) == {"t1": 1234, "t2": 1234}


def test_apply_hello_takes_camera_bounds() -> None:
    camera = Camera(mac="AA")
    adoption.apply_hello(
        camera,
        {
            "fwVersion": "5.3.95",
            "features": {
                "pan": {"steps": {"min": 500, "max": 35500}},
                "tilt": {"steps": {"min": 8000, "max": 18000}},
                "zoom": {"steps": {"min": 0, "max": 730}},
                "smartDetect": ["person", "vehicle"],
                "audioCodecs": ["aac", "opus"],
            },
        },
    )
    assert camera.is_ptz
    assert camera.firmware == "5.3.95"
    assert camera.smart_detect == ["person", "vehicle"]
    assert Codec.AAC in camera.audio_codecs


def test_video_settings_arm_and_audio_block() -> None:
    camera = Camera(mac="AA", tracks=adoption.track_defaults())
    payload = adoption.video_settings(camera, "10.0.0.1", 7550, ["video1"])
    assert payload["audio"] == {"bitRate": 64_000, "volume": 100}
    track = payload["video"]["video1"]
    assert track["type"] == "h264"  # default codec
    assert track["avSerializer"]["destinations"][0].startswith("tcp://10.0.0.1:7550")
    assert track["avSerializer"]["parameters"]["withOpus"] is True


def test_disarm_clears_destinations() -> None:
    payload = adoption.disarm(["video1"])
    assert payload["video"]["video1"]["avSerializer"]["destinations"] == []


def test_osd_drops_the_logo_but_keeps_the_overlay() -> None:
    payload = adoption.osd_settings("front door")
    assert payload["enableOverlay"] == 1
    assert payload["_1"]["enableLogo"] == 0
    assert payload["_1"]["tag"] == "front door"


def test_service_verb_is_parameterised() -> None:
    assert adoption.service("ssh", start=True) == ("StartService", {"service": "ssh"})
    assert adoption.service("ssh", start=False) == ("StopService", {"service": "ssh"})


def test_snapshot_request_carries_upload_url() -> None:
    payload = adoption.snapshot_request("https://host:7444/internal/camera-upload/tok")
    assert payload["what"] == "snapshot"
    assert payload["uri"].endswith("/tok")


def test_sequence_is_ack_gated() -> None:
    ids = envelope.Ids()
    seq = adoption.Sequence(
        steps=[adoption.Step("A", {}), adoption.Step("B", {})], ids=ids
    )
    first = seq.next_message()
    assert first is not None and first.function_name == "A"
    # Nothing more is released while an ack is outstanding.
    assert seq.next_message() is None
    ack = envelope.Envelope(
        function_name="A", payload={}, message_id=1, in_response_to=first.message_id,
        sender=envelope.CAMERA,
    )
    assert seq.on_reply(ack)
    second = seq.next_message()
    assert second is not None and second.function_name == "B"


def test_sequence_ignores_unrelated_reply() -> None:
    ids = envelope.Ids()
    seq = adoption.Sequence(steps=[adoption.Step("A", {})], ids=ids)
    seq.next_message()
    stray = envelope.Envelope(
        function_name="A", payload={}, message_id=5, in_response_to=999,
        sender=envelope.CAMERA,
    )
    assert not seq.on_reply(stray)
    assert seq.waiting_for is not None


def test_sequence_steps_past_unacked_verb() -> None:
    ids = envelope.Ids()
    seq = adoption.Sequence(steps=[adoption.Step("A", {}), adoption.Step("B", {})], ids=ids)
    seq.next_message()
    seq.on_timeout()
    nxt = seq.next_message()
    assert nxt is not None and nxt.function_name == "B"


def test_sequence_completes() -> None:
    ids = envelope.Ids()
    seq = adoption.Sequence(steps=[adoption.Step("A", {}, expect_ack=False)], ids=ids)
    seq.next_message()
    assert seq.done


def test_suite_starts_by_asking_position() -> None:
    camera = Camera(mac="AA", tracks=adoption.track_defaults())
    steps = adoption.suite(camera, "10.0.0.1", 7550, ["video1"])
    assert steps[0].name == ptz.GET_POSITION
    assert any(s.name == adoption.CHANGE_VIDEO for s in steps)
