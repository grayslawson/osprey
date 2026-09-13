"""Adoption end to end against a fake camera. No socket, no TLS, no hardware.

Drives the real Controller state machine, so the sequencing rules are exercised
rather than described.
"""

from __future__ import annotations

import threading
import selectors
import time
import logging
from pathlib import Path
from typing import Any

import pytest

import adoption
import controller
from unifiwire import envelope
import ptz
from unifiwire import ws
from model import Camera, Position

HELLO_PAYLOAD: dict[str, Any] = {
    "fwVersion": "5.3.95",
    "features": {
        "pan": {"steps": {"min": 500, "max": 35500}},
        "tilt": {"steps": {"min": 8000, "max": 18000}},
        "zoom": {"steps": {"min": 0, "max": 730}},
        "mic": 1,
        "smartDetect": ["person", "vehicle"],
        "audioCodecs": ["aac", "opus"],
    },
}


class Wire:
    """Stands in for the socket, recording what we would have sent."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        reader = ws.FrameReader()
        for frame in reader.feed(data):
            self.frames.append(frame.payload)

    def close(self) -> None:  # pragma: no cover - never asserted on
        pass

    def sent(self) -> list[envelope.Envelope]:
        out: list[envelope.Envelope] = []
        for raw in self.frames:
            try:
                out.append(envelope.decode(raw))
            except envelope.DecodeError:
                continue  # pings carry no payload
        return out

    def names(self) -> list[str]:
        return [m.function_name for m in self.sent()]


def _upgrade(subprotocol: str = "secure_transfer") -> ws.Upgrade:
    return ws.Upgrade(
        path=ws.CONTROL_PATH,
        key="dGhlIHNhbXBsZSBub25jZQ==",
        subprotocol=subprotocol,
        headers={"camera-mac": "AABBCCDDEEFF", "camera-model": "UVC G5 PTZ", "adopted": "false"},
    )


def _harness(subprotocol: str = "secure_transfer") -> tuple[controller.Controller, controller.Session, Wire]:
    ctl = controller.Controller(cert=Path("unused.pem"), ingest_host="10.0.0.1")
    wire = Wire()
    camera = Camera(mac="AABBCCDDEEFF", tracks=adoption.track_defaults())
    ctl.cameras[camera.mac] = camera
    session = controller.Session(
        sock=wire,  # type: ignore[arg-type]  # only sendall/close are used
        upgrade=_upgrade(subprotocol),
        camera=camera,
    )
    ctl._sessions[wire] = session  # type: ignore[index]
    return ctl, session, wire


def _ptz_harness(wire: Wire | None = None) -> tuple[controller.Controller, controller.Session, Wire]:
    ctl, session, default_wire = _harness(subprotocol=ptz.PTZ_SUBPROTOCOL)
    adoption.apply_hello(session.camera, HELLO_PAYLOAD)
    session.camera.motion.connect()
    if wire is None:
        return ctl, session, default_wire
    del ctl._sessions[default_wire]  # type: ignore[arg-type]
    session.sock = wire  # type: ignore[assignment]
    ctl._sessions[wire] = session  # type: ignore[index]
    return ctl, session, wire


def _from_camera(name: str, payload: dict[str, Any], mid: int, reply_to: int = 0) -> envelope.Envelope:
    return envelope.Envelope(
        function_name=name,
        payload=payload,
        message_id=mid,
        in_response_to=reply_to,
        sender=envelope.CAMERA,
        recipient=envelope.CONTROLLER,
    )


def _ack_everything(ctl: controller.Controller, session: controller.Session, wire: Wire) -> None:
    """Play a compliant camera: acknowledge each settings message in turn."""
    for _ in range(20):
        sequence = session.sequence
        if sequence is None or sequence.waiting_for is None:
            return
        outstanding = sequence.waiting_for
        pending = next(m for m in wire.sent() if m.message_id == outstanding)
        ctl._dispatch(session, _from_camera(pending.function_name, {}, 9000 + outstanding, outstanding))


def test_hello_is_answered_before_anything_else() -> None:
    ctl, session, wire = _harness()
    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 81068042))
    sent = wire.sent()
    assert sent[0].function_name == envelope.HELLO
    assert sent[0].in_response_to == 81068042, "the reply must carry the camera's own id"
    assert sent[1].function_name == envelope.PARAM_AGREEMENT


def test_hello_reply_takes_bounds_from_the_camera() -> None:
    ctl, session, _ = _harness()
    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 1))
    assert session.camera.is_ptz
    assert session.camera.pan_range.maximum == 35500
    assert session.camera.firmware == "5.3.95"


def test_settings_are_released_one_at_a_time() -> None:
    ctl, session, wire = _harness()
    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 1))
    after_hello = len(wire.frames)
    # A second unrelated frame must not shake another settings message loose.
    ctl._dispatch(session, _from_camera("EventPoorNetwork", {}, 2))
    assert len(wire.frames) == after_hello


def test_full_sequence_reaches_adopted_and_asks_for_ptz() -> None:
    adopted: list[Camera] = []
    ctl, session, wire = _harness()
    ctl.on_adopted = adopted.append
    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 1))
    _ack_everything(ctl, session, wire)
    assert session.camera.adopted
    assert adopted == [session.camera]
    names = wire.names()
    assert ptz.ENABLE_PTZ in names, "a PTZ camera should be asked to open its channel"
    enable = next(m for m in wire.sent() if m.function_name == ptz.ENABLE_PTZ)
    assert enable.payload["uri"].endswith(ws.CONTROL_PATH)


def test_an_adopted_camera_is_asked_to_reopen_its_ptz_channel() -> None:
    """A restarted camera has no ptz1 socket even though its model is adopted."""
    ctl, session, wire = _harness()
    session.camera.adopted = True
    adopted: list[Camera] = []
    ctl.on_adopted = adopted.append

    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 1))
    _ack_everything(ctl, session, wire)

    assert ptz.ENABLE_PTZ in wire.names()
    assert adopted == [], "reconnection must not report a second adoption"


def test_the_ptz_url_names_the_port_we_are_actually_on() -> None:
    """Without the port the camera dials 7442 — which may be another controller."""
    ctl, session, wire = _harness()
    ctl.control_port = 17442
    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 1))
    _ack_everything(ctl, session, wire)
    enable = next(m for m in wire.sent() if m.function_name == ptz.ENABLE_PTZ)
    assert enable.payload["uri"] == f"wss://10.0.0.1:17442{ws.CONTROL_PATH}"


def test_non_ptz_camera_is_not_asked_for_a_ptz_channel() -> None:
    ctl, session, wire = _harness()
    payload = {"features": {"mic": 1}}  # no motor bounds announced
    ctl._dispatch(session, _from_camera(envelope.HELLO, payload, 1))
    _ack_everything(ctl, session, wire)
    assert session.camera.adopted
    assert ptz.ENABLE_PTZ not in wire.names()


def test_time_sync_is_answered_in_band() -> None:
    ctl, session, wire = _harness()
    ctl._dispatch(session, _from_camera(envelope.TIME_SYNC, {"timeDelta": 0}, 77))
    reply = next(m for m in wire.sent() if m.function_name == envelope.TIME_SYNC)
    assert reply.in_response_to == 77
    assert set(reply.payload) == {"t1", "t2"}


def test_motor_state_updates_position_and_notifies() -> None:
    seen: list[Camera] = []
    ctl, session, _ = _harness()
    ctl.on_motion = seen.append
    ctl._dispatch(
        session,
        _from_camera(
            ptz.EVENT_MOTOR_STATE,
            {"state": {"activity": 0, "position": {"pan": 23502, "tilt": 8000, "zoom": 0, "focus": 58}}},
            5,
        ),
    )
    assert session.camera.motion.position.pan == 23502
    assert session.camera.motion.settled
    assert seen == [session.camera]


def test_position_reply_recovers_motion_state_after_ptz_reconnect() -> None:
    seen: list[Camera] = []
    ctl, session, _ = _harness()
    ctl.on_motion = seen.append
    session.camera.motion.connect()
    session.camera.motion.reconnect()

    ctl._dispatch(
        session,
        _from_camera(
            ptz.GET_POSITION,
            {
                "activity": 0,
                "position": {
                    "pan": 23502,
                    "tilt": 8000,
                    "zoom": 0,
                    "focus": 58,
                },
            },
            6,
            reply_to=10000,
        ),
    )

    assert session.camera.motion.position.pan == 23502
    assert session.camera.motion.status == "IDLE"
    assert seen == [session.camera]


def test_ptz_socket_loss_rearms_once_and_marks_motion_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lost ptz1 socket is recovered through its still-live management socket."""
    caplog.set_level(logging.INFO, logger="cuckoo.control")
    ctl, control, wire = _harness()
    adoption.apply_hello(control.camera, HELLO_PAYLOAD)
    control.camera.adopted = True
    control.camera.motion.connect()
    ptz_wire = Wire()
    ptz_session = controller.Session(
        sock=ptz_wire,  # type: ignore[arg-type]
        upgrade=_upgrade(ptz.PTZ_SUBPROTOCOL),
        camera=control.camera,
    )
    ctl._sessions[ptz_wire] = ptz_session  # type: ignore[index]

    selector = selectors.DefaultSelector()
    ctl._drop(ptz_wire, selector)  # type: ignore[arg-type]
    now = time.monotonic()
    ctl._recover_ptz(now)

    assert control.camera.motion.status == "UNKNOWN"
    assert wire.names() == [ptz.DISABLE_PTZ, ptz.ENABLE_PTZ]
    # The state machine, not each selector tick, owns this recovery attempt.
    ctl._recover_ptz(now + 0.1)
    assert wire.names() == [ptz.DISABLE_PTZ, ptz.ENABLE_PTZ]
    assert "PTZ channel unavailable" in caplog.text
    assert "PTZ recovery waiting for callback" in caplog.text


def test_ptz_recovery_clears_when_the_callback_returns() -> None:
    ctl, control, _ = _harness()
    adoption.apply_hello(control.camera, HELLO_PAYLOAD)
    control.camera.adopted = True
    now = time.monotonic()
    ctl._request_ptz_recovery(control, now)
    ctl._recover_ptz(now)

    ptz_wire = Wire()
    ptz_session = controller.Session(
        sock=ptz_wire,  # type: ignore[arg-type]
        upgrade=_upgrade(ptz.PTZ_SUBPROTOCOL),
        camera=control.camera,
    )
    control.camera.motion.open_channel()
    ctl._sessions[ptz_wire] = ptz_session  # type: ignore[index]
    ctl._recover_ptz(now + 0.1)

    assert control.camera.mac not in ctl._ptz_recovery
    assert control.camera.motion.status == "IDLE"


def test_ptz_recovery_retries_with_backoff_then_stops(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(controller, "PTZ_RECOVERY_CALLBACK_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(controller, "PTZ_RECOVERY_INITIAL_BACKOFF_SEC", 1.0)
    monkeypatch.setattr(controller, "PTZ_RECOVERY_MAX_ATTEMPTS", 2)
    ctl, control, wire = _harness()
    adoption.apply_hello(control.camera, HELLO_PAYLOAD)
    control.camera.adopted = True
    now = time.monotonic()
    ctl._request_ptz_recovery(control, now)

    ctl._recover_ptz(now)  # first Disable -> Enable
    ctl._recover_ptz(now + 1.1)  # callback timeout, then back off
    recovery = ctl._ptz_recovery[control.camera.mac]
    retry_at = recovery.next_attempt_at
    ctl._recover_ptz(retry_at)  # second Disable -> Enable
    ctl._recover_ptz(retry_at + 1.1)  # second callback timeout
    recovery = ctl._ptz_recovery[control.camera.mac]
    ctl._recover_ptz(recovery.next_attempt_at)  # bounded terminal timeout

    assert wire.names() == [
        ptz.DISABLE_PTZ,
        ptz.ENABLE_PTZ,
        ptz.DISABLE_PTZ,
        ptz.ENABLE_PTZ,
    ]
    assert ctl._ptz_recovery[control.camera.mac].exhausted
    assert "PTZ recovery callback timed out" in caplog.text
    assert "PTZ recovery timed out" in caplog.text


def test_unacked_step_is_stepped_past() -> None:
    ctl, session, wire = _harness()
    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 1))
    sequence = session.sequence
    assert sequence is not None
    stuck = sequence.waiting_for
    session.ack_deadline = 1.0  # already elapsed
    ctl._tick()
    assert sequence.waiting_for != stuck


def test_move_writes_a_scratch_preset_then_goes() -> None:
    ctl, session, wire = _ptz_harness()
    target = Position(pan=1000, tilt=9000, zoom=0, focus=50)

    assert ctl.move("AABBCCDDEEFF", target)

    payloads = [m.payload for m in wire.sent() if m.function_name == ptz.PRESET]
    assert [p["action"] for p in payloads] == ["config", "go"]
    assert session.camera.motion.status == "MOVING"
    session.camera.motion.update(Position(), activity=0)
    assert session.camera.motion.status == "MOVING", "an older idle event must not settle this move"
    session.camera.motion.update(target, activity=0)
    assert session.camera.motion.status == "IDLE"


def test_ignored_motor_activity_at_target_releases_the_next_calibration_move() -> None:
    """G5 ignores a stale activity word after a tightly adjacent zoom step."""
    ctl, session, _ = _ptz_harness()
    target = Position(pan=19635, tilt=12334, zoom=730, focus=109)
    assert ctl.move("AABBCCDDEEFF", target)

    ctl._dispatch(
        session,
        _from_camera(
            ptz.EVENT_MOTOR_STATE,
            {
                "ignoreActivity": True,
                "state": {
                    "activity": 16,
                    "position": {
                        "pan": 19635,
                        "tilt": 12334,
                        "zoom": 730,
                        "focus": 115,
                    },
                },
            },
            7,
        ),
    )

    assert session.camera.motion.status == "IDLE"
    assert ctl.move(
        "AABBCCDDEEFF", Position(pan=19635, tilt=12334, zoom=723, focus=115)
    )


def test_move_is_not_misrouted_to_the_management_channel() -> None:
    ctl, _, wire = _harness()

    assert not ctl.move("AABBCCDDEEFF", Position(pan=1000))
    assert ptz.PRESET not in wire.names()


def test_concurrent_moves_keep_configure_and_go_atomic() -> None:
    class BlockingWire(Wire):
        def __init__(self) -> None:
            super().__init__()
            self.first_write = threading.Event()
            self.release = threading.Event()

        def sendall(self, data: bytes) -> None:
            super().sendall(data)
            if not self.first_write.is_set():
                self.first_write.set()
                assert self.release.wait(timeout=2)

    wire = BlockingWire()
    ctl, _, _ = _ptz_harness(wire)
    results: list[bool] = []
    first = threading.Thread(target=lambda: results.append(ctl.move("AABBCCDDEEFF", Position(pan=1))))
    second = threading.Thread(target=lambda: results.append(ctl.move("AABBCCDDEEFF", Position(pan=2))))

    first.start()
    assert wire.first_write.wait(timeout=2)
    second.start()
    wire.release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert sorted(results) == [False, True]
    payloads = [m.payload for m in wire.sent() if m.function_name == ptz.PRESET]
    assert [p["action"] for p in payloads] == ["config", "go"]


def test_failed_move_write_rolls_back_to_idle() -> None:
    class FailingWire(Wire):
        def sendall(self, data: bytes) -> None:
            raise OSError("test write failure")

    ctl, session, _ = _ptz_harness(FailingWire())

    assert not ctl.move("AABBCCDDEEFF", Position(pan=1))
    assert session.camera.motion.status == "IDLE"


def test_move_reports_failure_for_unknown_camera() -> None:
    ctl, _, _ = _harness()
    assert not ctl.move("NOPE", Position())


def test_ptz_channel_is_recognised_by_subprotocol() -> None:
    _, session, _ = _harness(subprotocol=ptz.PTZ_SUBPROTOCOL)
    assert session.is_ptz_channel


def test_frames_from_ourselves_are_ignored() -> None:
    """Only the camera drives the machine; our own echo must not."""
    ctl, session, wire = _harness()
    ours = envelope.Envelope(
        function_name=envelope.HELLO, payload=HELLO_PAYLOAD, message_id=1,
        sender=envelope.CONTROLLER,
    )
    ctl._on_frame(session, ws.Frame(ws.Opcode.BINARY, ours.to_json()))
    assert wire.frames == []


def test_malformed_frame_does_not_break_the_session() -> None:
    ctl, session, wire = _harness()
    ctl._on_frame(session, ws.Frame(ws.Opcode.BINARY, b"\xff\xfe not json"))
    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 1))
    assert session.camera.pan_range.maximum == 35500


def test_ping_is_answered_with_pong() -> None:
    ctl, session, wire = _harness()
    ctl._on_frame(session, ws.Frame(ws.Opcode.PING, b"x"))
    assert wire.frames == [b"x"]


@pytest.mark.parametrize("subprotocol", ["secure_transfer", "ptz1"])
def test_handshake_echoes_whichever_subprotocol_arrives(subprotocol: str) -> None:
    response = ws.handshake_response(_upgrade(subprotocol))
    assert f"Sec-WebSocket-Protocol: {subprotocol}".encode() in response


def test_every_timesync_is_answered_reply_or_not() -> None:
    """The camera and controller ping-pong timeSync until the clock converges;
    a controller that answers only the first stalls it, and no hello ever comes."""
    ctl, session, wire = _harness()
    ctl._dispatch(session, _from_camera(envelope.TIME_SYNC, {"timeDelta": 0}, 500))
    # the camera's next timeSync is a reply (references our messageId) — still answered
    ctl._dispatch(session, _from_camera(envelope.TIME_SYNC, {}, 501, reply_to=10000))
    answers = [m for m in wire.sent() if m.function_name == envelope.TIME_SYNC]
    assert len(answers) == 2, "both timeSyncs answered, not just the unprompted one"
    assert {m.in_response_to for m in answers} == {500, 501}


def test_the_controller_does_not_speak_first() -> None:
    """The real controller never sends its own hello; it waits for the camera's."""
    ctl, session, wire = _harness()
    for mid in range(600, 610):
        ctl._dispatch(session, _from_camera(envelope.TIME_SYNC, {}, mid))
    assert not any(m.function_name == envelope.HELLO for m in wire.sent())


def test_camera_mac_normalisation_accepts_common_display_forms() -> None:
    assert controller.normalise_mac("aa:bb:cc:dd:ee:ff") == "AABBCCDDEEFF"
    assert controller.normalise_mac("AA-BB-CC-DD-EE-FF") == "AABBCCDDEEFF"


def test_invalid_camera_mac_is_rejected() -> None:
    with pytest.raises(ValueError):
        controller.normalise_mac("not-a-camera")


def test_the_cameras_hello_still_drives_the_rest() -> None:
    """Once the camera introduces itself, adoption proceeds as normal."""
    ctl, session, wire = _harness()
    ctl._dispatch(session, _from_camera(envelope.TIME_SYNC, {}, 1))
    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 900))
    assert session.camera.pan_range.maximum == 35500
    assert session.sequence is not None



def test_a_camera_that_opens_with_hello_is_still_adopted_once() -> None:
    """Some cameras hello first; the sequence must not run twice."""
    ctl, session, wire = _harness()
    ctl._dispatch(session, _from_camera(envelope.HELLO, HELLO_PAYLOAD, 1))
    ctl._dispatch(session, _from_camera(envelope.TIME_SYNC, {}, 2))
    sequences_started = sum(1 for m in wire.sent() if m.function_name == envelope.PARAM_AGREEMENT)
    assert sequences_started == 1, "one adoption, whichever message arrives first"
