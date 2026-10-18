"""PTZ liveness: motion settle rules, the ptz1 watchdog, recovery, presets.

Regression coverage for the incident where the ptz1 websocket stayed up (pings
flowing) while the camera stopped answering GetCurrentPosition, leaving ONVIF
GetStatus at UNKNOWN "motor state timeout" until Osprey was restarted. Every
test here uses fake sockets and an explicit clock; none needs a camera.
"""

from __future__ import annotations

import json
import logging
import selectors
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import adoption
import controller
import model
import ptz
from model import Camera, Position, Preset
from presets import PresetStore
from test_adoption_flow import (
    HELLO_PAYLOAD,
    Wire,
    _from_camera,
    _harness,
    _upgrade,
)
from unifiwire import ws


class FakeTime:
    """Stands in for the `time` module inside model.py."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


def _needs_poll(camera: Camera) -> bool:
    return camera.motion.needs_poll


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(model, "time", fake)
    return fake


def _motion_camera() -> Camera:
    camera = Camera(mac="AA")
    camera.motion.connect()
    camera.motion.update(Position(pan=1000, tilt=9000, zoom=100, focus=5), activity=0)
    return camera


# ----------------------------------------------------------------- motion model


def test_a_move_to_the_current_pose_does_not_hold_the_reservation(clock: FakeTime) -> None:
    """No-op moves used to report IDLE while refusing every move as busy."""
    camera = _motion_camera()
    here = camera.motion.position

    assert camera.motion.begin(here)
    assert camera.motion.status == "IDLE"
    assert camera.motion.settled
    assert camera.motion.begin(Position(pan=2000, tilt=9000, zoom=100, focus=5))
    assert camera.motion.status == "MOVING"


def test_unflagged_activity_at_target_latches_moving_only_for_the_grace(
    clock: FakeTime,
) -> None:
    camera = _motion_camera()
    target = Position(pan=2000, tilt=9000, zoom=100, focus=5)
    assert camera.motion.begin(target)

    camera.motion.update(target, activity=16)
    # Race protection: genuine motion at the target is briefly authoritative.
    assert camera.motion.status == "MOVING"
    assert not camera.motion.begin(Position(pan=3000))

    clock.now += model.AT_TARGET_ACTIVITY_GRACE_SEC - 0.1
    assert camera.motion.status == "MOVING"
    clock.now += 0.2
    assert camera.motion.status == "IDLE", "a settled-at-target head must not latch MOVING"
    assert camera.motion.begin(Position(pan=3000, tilt=9000, zoom=100, focus=5))


def test_at_target_grace_restarts_if_the_head_leaves_the_target(clock: FakeTime) -> None:
    camera = _motion_camera()
    target = Position(pan=2000, tilt=9000, zoom=100, focus=5)
    assert camera.motion.begin(target)
    camera.motion.update(target, activity=16)
    clock.now += 1.5
    camera.motion.update(Position(pan=1800, tilt=9000, zoom=100), activity=16)
    clock.now += 1.0
    assert camera.motion.status == "MOVING"


def test_terminal_frame_off_target_ends_the_move_with_a_warning(
    clock: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    """The head stopping >4 steps from the target used to latch MOVING."""
    caplog.set_level(logging.WARNING, logger="cuckoo.motion")
    camera = _motion_camera()
    target = Position(pan=5000, tilt=9000, zoom=100, focus=5)
    assert camera.motion.begin(target)

    camera.motion.update(Position(pan=3000, tilt=9000, zoom=100), activity=16)
    assert camera.motion.status == "MOVING"
    camera.motion.update(Position(pan=4980, tilt=9000, zoom=100), activity=0)

    assert camera.motion.status == "IDLE"
    assert camera.motion.position.pan == 4980
    assert "settled off target" in caplog.text


def test_a_mid_move_position_reply_is_not_a_terminal_frame(clock: FakeTime) -> None:
    """GetCurrentPosition replies carry no activity word (parsed as 0)."""
    camera = _motion_camera()
    target = Position(pan=5000, tilt=9000, zoom=100, focus=5)
    assert camera.motion.begin(target)
    camera.motion.update(Position(pan=3000, tilt=9000, zoom=100), activity=16)

    camera.motion.update(Position(pan=3500, tilt=9000, zoom=100), activity=0, confirmed=True)

    assert camera.motion.status == "MOVING"


def test_trailing_noise_at_the_start_pose_does_not_arm_off_target_settle(
    clock: FakeTime,
) -> None:
    """Lens noise left over from the last move must not count as this move."""
    camera = _motion_camera()
    start = camera.motion.position
    assert camera.motion.begin(Position(pan=5000, tilt=9000, zoom=100, focus=5))

    camera.motion.update(start, activity=16)
    camera.motion.update(start, activity=0)

    assert camera.motion.status == "MOVING", "a stale idle frame must not settle the move"


def test_a_timeout_reports_unknown_asks_for_a_poll_and_a_reply_clears_it(
    clock: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="cuckoo.motion")
    camera = _motion_camera()
    assert camera.motion.begin(Position(pan=5000, tilt=9000, zoom=100, focus=5))
    before = camera.motion.needs_poll
    assert not before

    clock.now += model.MOTION_EVENT_TIMEOUT_SEC
    assert camera.motion.status == "UNKNOWN"
    assert camera.motion.error == "motor state timeout"
    assert camera.motion.needs_poll
    assert camera.motion.timeouts == 1
    assert "motor state timeout" in caplog.text

    camera.motion.update(Position(pan=5000, tilt=9000, zoom=100), activity=0, confirmed=True)
    assert camera.motion.status == "IDLE"
    position, pan_tilt, zoom, error = camera.motion.snapshot()
    assert (pan_tilt, zoom, error) == ("IDLE", "IDLE", None)
    assert not _needs_poll(camera)


def test_the_reservation_timeout_is_shorter_than_a_minute() -> None:
    assert model.MOTION_EVENT_TIMEOUT_SEC <= 20.0
    assert model.AT_TARGET_ACTIVITY_GRACE_SEC < 10.0, "must beat Frigate's 10 s wait"


def test_last_reply_age_tracks_motor_replies(clock: FakeTime) -> None:
    camera = Camera(mac="AA")
    camera.motion.connect()
    assert camera.motion.last_reply_age() is None
    camera.motion.update(Position(), activity=0)
    clock.now += 7.0
    assert camera.motion.last_reply_age() == pytest.approx(7.0)


# ------------------------------------------------------------------- watchdog


def _live(ctl: controller.Controller, wire: object) -> bool:
    return any(sock is wire for sock in ctl._sessions)


def _ptz_pair(
    **kwargs: Any,
) -> tuple[controller.Controller, controller.Session, Wire, controller.Session, Wire]:
    """A management session plus a live ptz1 session for the same camera."""
    ctl, control, control_wire = _harness()
    for key, value in kwargs.items():
        setattr(ctl, key, value)
    adoption.apply_hello(control.camera, HELLO_PAYLOAD)
    control.camera.adopted = True
    ptz_wire = Wire()
    ptz_session = controller.Session(
        sock=ptz_wire,  # type: ignore[arg-type]
        upgrade=_upgrade(ptz.PTZ_SUBPROTOCOL),
        camera=control.camera,
    )
    control.camera.motion.open_channel()
    ctl._sessions[ptz_wire] = ptz_session  # type: ignore[index]
    return ctl, control, control_wire, ptz_session, ptz_wire


def _position_reply(session: controller.Session, reply_to: int = 1) -> Any:
    return _from_camera(
        ptz.GET_POSITION,
        {"steps": {"pan": 18000, "tilt": 13000, "zoom": 0, "focus": 1}},
        900 + reply_to,
        reply_to=reply_to,
    )


def test_watchdog_probes_while_state_is_unknown() -> None:
    ctl, control, _, ptz_session, ptz_wire = _ptz_pair()
    control.camera.motion.reconnect()  # UNKNOWN until fresh state
    now = time.monotonic()

    ctl._watch_ptz(now)
    assert ptz_wire.names().count(ptz.GET_POSITION) == 1
    ctl._watch_ptz(now + 0.5)
    assert ptz_wire.names().count(ptz.GET_POSITION) == 1, "probes are rate limited"
    ctl._watch_ptz(now + ctl.watchdog.probe_interval_sec)
    assert ptz_wire.names().count(ptz.GET_POSITION) == 2

    ctl._dispatch(ptz_session, _position_reply(ptz_session))
    assert control.camera.motion.status == "IDLE"
    assert ptz_session.unanswered_polls == 0
    ctl._watch_ptz(now + 10)
    assert ptz_wire.names().count(ptz.GET_POSITION) == 2, "healthy idle channel is quiet"


def test_a_motor_timeout_is_healed_by_the_watchdog_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctl, control, _, ptz_session, ptz_wire = _ptz_pair()
    ctl._dispatch(ptz_session, _position_reply(ptz_session))
    assert ctl.move(control.camera.mac, Position(pan=1000, tilt=9000, zoom=0, focus=1))
    monkeypatch.setattr(control.camera.motion, "_deadline", 1.0)  # already elapsed
    assert control.camera.motion.status == "UNKNOWN"

    ctl._watch_ptz(time.monotonic())
    assert ptz_wire.names()[-1] == ptz.GET_POSITION
    ctl._dispatch(ptz_session, _position_reply(ptz_session, reply_to=2))

    assert control.camera.motion.status == "IDLE"
    assert control.camera.motion.error is None


def test_watchdog_recycles_after_three_unanswered_position_requests(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pings flowing is not liveness; unanswered position requests are."""
    caplog.set_level(logging.INFO, logger="cuckoo.control")
    ctl, control, control_wire, ptz_session, ptz_wire = _ptz_pair()
    mac = control.camera.mac
    now = time.monotonic()
    for offset in (0.0, 2.0):
        ptz_session.request_position(now + offset)
    ctl._watch_ptz(now + 4.5)
    assert _live(ctl, ptz_wire), "two unanswered requests are not yet enough"

    ptz_session.request_position(now + 4.0)
    ctl._watch_ptz(now + 4.5)

    assert not _live(ctl, ptz_wire)
    assert control.camera.motion.status == "UNKNOWN"
    assert "not responding" in (control.camera.motion.error or "")
    ctl._recover_ptz(now + 5.1)
    assert control_wire.names() == [ptz.DISABLE_PTZ, ptz.ENABLE_PTZ]
    health = ctl.ptz_health(mac)
    assert health["watchdog_recycles"] == 1
    assert health["healthy"] is False
    assert health["channel"] == "down"
    assert "PTZ watchdog" in caplog.text


def test_watchdog_recycles_after_thirty_seconds_of_silence() -> None:
    """A single unanswered request is recycled only once it has been silent long enough."""
    ctl, control, _, ptz_session, ptz_wire = _ptz_pair()
    now = time.monotonic()
    ptz_session.request_position(now)
    ctl._watch_ptz(now + ctl.watchdog.reply_timeout_sec - 0.5)
    assert _live(ctl, ptz_wire), "not before the silence bound"

    ctl._watch_ptz(now + ctl.watchdog.reply_timeout_sec)
    assert not _live(ctl, ptz_wire)
    assert ctl.ptz_health(control.camera.mac)["watchdog_recycles"] == 1


def test_rapid_ondemand_polls_from_frigate_coalesce() -> None:
    """A burst of GetStatus refreshes is one outstanding request, not many.

    Frigate polls several times a second while tracking; a camera that answers
    in half a second must not be recycled for it.
    """
    ctl, control, _, ptz_session, ptz_wire = _ptz_pair()
    for _ in range(5):
        assert ctl.poll_position(control.camera.mac)
    assert ptz_session.unanswered_polls == 1
    assert ptz_wire.names().count(ptz.GET_POSITION) == 1
    ctl._watch_ptz(ptz_session.position_requested_at + 1.0)
    assert _live(ctl, ptz_wire)


def test_ondemand_polls_still_trip_the_silence_bound() -> None:
    """Coalesced GetStatus refreshes still count toward the silence bound."""
    ctl, control, _, ptz_session, _ = _ptz_pair()
    assert ctl.poll_position(control.camera.mac)
    ctl._watch_ptz(ptz_session.position_requested_at + ctl.watchdog.reply_timeout_sec)
    assert ctl._ptz_session(control.camera.mac) is None


def test_a_push_event_counts_as_liveness() -> None:
    ctl, control, _, ptz_session, ptz_wire = _ptz_pair()
    now = time.monotonic()
    for offset in (0.0, 2.0, 4.0):
        ptz_session.request_position(now + offset)
    ctl._dispatch(
        ptz_session,
        _from_camera(
            ptz.EVENT_MOTOR_STATE,
            {"state": {"activity": 0, "position": {"pan": 1, "tilt": 2, "zoom": 0, "focus": 0}}},
            77,
        ),
    )
    ctl._watch_ptz(now + 30)
    assert _live(ctl, ptz_wire)


def test_repeated_recycles_back_off_and_share_the_attempt_budget() -> None:
    ctl, control, _, ptz_session, _ = _ptz_pair()
    mac = control.camera.mac
    now = time.monotonic()
    ctl._watchdog_strikes[mac] = 2  # two earlier recycles never got an answer
    for offset in (0.0, 2.0, 4.0):
        ptz_session.request_position(now + offset)

    ctl._watch_ptz(now + 6)

    recovery = ctl._ptz_recovery[mac]
    assert recovery.attempts == 2
    assert recovery.next_attempt_at > now + 6 + controller.PTZ_RECOVERY_INITIAL_BACKOFF_SEC


def test_a_reply_after_recovery_resets_the_strike_count() -> None:
    ctl, control, _, ptz_session, _ = _ptz_pair()
    ctl._watchdog_strikes[control.camera.mac] = 3
    ctl._dispatch(ptz_session, _position_reply(ptz_session))
    assert control.camera.mac not in ctl._watchdog_strikes


def test_an_exhausted_recovery_is_rearmed_after_the_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(controller, "PTZ_RECOVERY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(controller, "PTZ_RECOVERY_CALLBACK_TIMEOUT_SEC", 1.0)
    ctl, control, wire = _harness()
    adoption.apply_hello(control.camera, HELLO_PAYLOAD)
    mac = control.camera.mac
    now = time.monotonic()
    ctl._request_ptz_recovery(control, now)
    ctl._recover_ptz(now)
    ctl._recover_ptz(now + 1.1)
    recovery = ctl._ptz_recovery[mac]
    ctl._recover_ptz(recovery.next_attempt_at)
    assert recovery.exhausted
    assert wire.names()[-1] == ptz.REBOOT
    sent = len(wire.names())

    ctl._recover_ptz(recovery.exhausted_at + 1)
    assert len(wire.names()) == sent, "no retries during the cooldown"
    ctl._recover_ptz(recovery.exhausted_at + controller.PTZ_RECOVERY_REARM_SEC)
    assert not ctl._ptz_recovery[mac].exhausted
    assert wire.names()[-2:] == [ptz.DISABLE_PTZ, ptz.ENABLE_PTZ]


def test_operator_recovery_recycles_the_live_channel() -> None:
    ctl, control, control_wire, _, ptz_wire = _ptz_pair()
    mac = control.camera.mac

    assert ctl.recover_ptz(mac)
    assert _live(ctl, ptz_wire), "work happens on the control loop, not the caller"
    ctl._tick()

    assert not _live(ctl, ptz_wire)
    assert control.camera.motion.error == "operator requested PTZ recovery"
    assert control_wire.names()[-2:] == [ptz.DISABLE_PTZ, ptz.ENABLE_PTZ]


def test_operator_recovery_restarts_an_exhausted_sequence() -> None:
    ctl, control, control_wire = _harness()
    adoption.apply_hello(control.camera, HELLO_PAYLOAD)
    mac = control.camera.mac
    ctl._ptz_recovery[mac] = controller.PtzRecovery(
        attempts=5, exhausted=True, exhausted_at=time.monotonic()
    )

    assert ctl.recover_ptz(mac)
    ctl._tick()

    assert control_wire.names()[-2:] == [ptz.DISABLE_PTZ, ptz.ENABLE_PTZ]
    assert ctl._ptz_recovery[mac].attempts == 1


def test_operator_recovery_needs_a_management_channel() -> None:
    ctl = controller.Controller(cert=Path("unused.pem"), ingest_host="10.0.0.1")
    assert not ctl.recover_ptz("AABBCCDDEEFF")


def test_watchdog_config_is_bounded() -> None:
    with pytest.raises(ValueError):
        controller.PtzWatchdog(reply_timeout_sec=0.0)
    with pytest.raises(ValueError):
        controller.PtzWatchdog(max_unanswered=0)
    with pytest.raises(ValueError):
        controller.PtzWatchdog(idle_probe_sec=1.0)


# --------------------------------------------------------------- socket hygiene


class ReadableWire(Wire):
    """A socket the read loop can recv from."""

    def __init__(self, inbound: bytes) -> None:
        super().__init__()
        self.inbound = inbound
        self.closed = False

    def recv(self, _size: int) -> bytes:
        data, self.inbound = self.inbound, b""
        return data

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return -1


def test_a_close_frame_drops_the_session_and_starts_recovery() -> None:
    """Closing without _drop() left a dead session and an fd-reuse crash."""
    ctl, control, control_wire = _harness()
    adoption.apply_hello(control.camera, HELLO_PAYLOAD)
    control.camera.motion.connect()
    wire = ReadableWire(ws.encode_frame(b"", ws.Opcode.CLOSE, mask=True))
    session = controller.Session(
        sock=wire,  # type: ignore[arg-type]
        upgrade=_upgrade(ptz.PTZ_SUBPROTOCOL),
        camera=control.camera,
    )
    ctl._sessions[wire] = session  # type: ignore[index]

    ctl._readable(wire, selectors.DefaultSelector())  # type: ignore[arg-type]

    assert not _live(ctl, wire)
    assert wire.closed
    assert control.camera.motion.status == "UNKNOWN"
    assert control.camera.mac in ctl._ptz_recovery


def test_pings_and_pongs_share_the_session_write_lock() -> None:
    ctl, session, wire = _harness()
    finished = threading.Event()
    with session._write_lock:
        def write() -> None:
            session.pong(b"x")
            finished.set()

        writer = threading.Thread(target=write)
        writer.start()
        assert not finished.wait(0.1), "a pong must not interleave with a locked write"
    writer.join(timeout=2)
    assert finished.is_set()
    session.ping()
    assert len(wire.frames) == 2


def test_a_failed_write_marks_the_session_broken_and_drops_it() -> None:
    class Truncating(Wire):
        def __init__(self) -> None:
            super().__init__()
            self.shut = False

        def sendall(self, data: bytes) -> None:
            raise TimeoutError("partial write")

        def shutdown(self, _how: int) -> None:
            self.shut = True

    ctl, session, _ = _harness()
    wire = Truncating()
    del ctl._sessions[session.sock]
    session.sock = wire  # type: ignore[assignment]
    ctl._sessions[wire] = session  # type: ignore[index]

    with pytest.raises(OSError):
        session.request(ptz.GET_POSITION, ptz.get_position())
    assert session.broken and wire.shut
    with pytest.raises(OSError):
        session.ping()
    ctl._tick()
    assert not _live(ctl, wire)


# ------------------------------------------------------------------- presets


def test_preset_store_round_trips_and_ignores_garbage(tmp_path: Path) -> None:
    path = tmp_path / "presets.json"
    store = PresetStore(path)
    assert store.load("AABBCCDDEEFF") == {}
    assert store.save("AABBCCDDEEFF", {3: Preset(3, "gate", Position(1, 2, 3, 4))})
    assert store.load("AABBCCDDEEFF")[3] == Preset(3, "gate", Position(1, 2, 3, 4))

    path.write_text(json.dumps({"cameras": {"AABBCCDDEEFF": {
        "x": {"name": "bad"}, "4": {"name": "", "pan": 1, "tilt": 1, "zoom": 1, "focus": 1},
        "5": {"name": "ok", "pan": 1, "tilt": 2, "zoom": 3, "focus": True},
        "6": {"name": "door", "pan": 1, "tilt": 2, "zoom": 3, "focus": 4},
    }}}))
    assert list(store.load("AABBCCDDEEFF")) == [6]
    path.write_text("{not json")
    assert store.load("AABBCCDDEEFF") == {}


def test_presets_survive_a_controller_restart(tmp_path: Path) -> None:
    store = PresetStore(tmp_path / "presets.json")
    ctl, control, _, _, ptz_wire = _ptz_pair(preset_store=store, preset_ack_timeout=0.01)
    slot = ctl.set_preset(control.camera.mac, "gate")
    assert slot is not None
    assert any(m.payload.get("action") == "config" for m in ptz_wire.sent())

    restarted = controller.Controller(
        cert=Path("unused.pem"), ingest_host="10.0.0.1", preset_store=store
    )
    camera = restarted._camera_for(control.camera.mac)
    assert camera.presets[slot].name == "gate"

    assert restarted.remove_preset(camera.mac, slot)
    assert PresetStore(tmp_path / "presets.json").load(camera.mac) == {}


def test_restored_presets_are_reasserted_on_the_camera_once(tmp_path: Path) -> None:
    ctl, control, _, ptz_session, ptz_wire = _ptz_pair()
    control.camera.presets = {0: Preset(0, "home", Position(1, 2, 3, 4))}
    ctl._sync_presets(ptz_session)
    ctl._sync_presets(ptz_session)
    configs = [m for m in ptz_wire.sent() if m.payload.get("action") == "config"]
    assert len(configs) == 1
    assert configs[0].payload["items"][0]["name"] == "home"


def test_preset_slots_skip_scratch_and_never_collide() -> None:
    ctl, control, _, _, _ = _ptz_pair(preset_ack_timeout=0.01)
    camera = control.camera
    camera.presets = {i: Preset(i, f"p{i}", Position()) for i in range(ptz.SCRATCH_INDEX)}
    camera.presets[ptz.SCRATCH_INDEX + 1] = Preset(ptz.SCRATCH_INDEX + 1, "keep", Position())

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(controller, "MAX_PRESETS_PER_CAMERA", 255)
        slot = ctl.set_preset(camera.mac, "new")
        explicit = ctl.set_preset(camera.mac, "scratch?", ptz.SCRATCH_INDEX)

    assert slot == ptz.SCRATCH_INDEX + 2
    assert camera.presets[ptz.SCRATCH_INDEX + 1].name == "keep"
    assert explicit == ptz.SCRATCH_INDEX + 3


def test_a_refused_preset_write_is_reported_as_a_failure() -> None:
    ctl, control, _, ptz_session, _ = _ptz_pair(preset_ack_timeout=2.0)

    class Refusing(Wire):
        def sendall(self, data: bytes) -> None:
            super().sendall(data)
            sent = self.sent()[-1]
            reply = _from_camera(ptz.PRESET, {"error": "slot locked"}, 5000, sent.message_id)
            threading.Timer(0.02, ctl._dispatch, args=(ptz_session, reply)).start()

    wire = Refusing()
    del ctl._sessions[ptz_session.sock]
    ptz_session.sock = wire  # type: ignore[assignment]
    ctl._sessions[wire] = ptz_session  # type: ignore[index]

    assert ctl.set_preset(control.camera.mac, "gate") is None
    assert control.camera.presets == {}


def test_an_unacknowledged_preset_write_is_kept_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="cuckoo.control")
    ctl, control, _, _, _ = _ptz_pair(preset_ack_timeout=0.01)
    assert ctl.set_preset(control.camera.mac, "gate") == 0
    assert "not acknowledged" in caplog.text


def test_first_confirmed_position_saves_home_when_none_exists(tmp_path: Path) -> None:
    store = PresetStore(tmp_path / "presets.json")
    ctl, control, _, ptz_session, ptz_wire = _ptz_pair(preset_store=store)

    ctl._dispatch(ptz_session, _position_reply(ptz_session))
    ctl._dispatch(ptz_session, _position_reply(ptz_session, reply_to=2))

    homes = [p for p in control.camera.presets.values() if p.name == "home"]
    assert len(homes) == 1
    assert homes[0].position.pan == 18000
    configs = [m for m in ptz_wire.sent() if m.payload.get("action") == "config"]
    assert len(configs) == 1
    assert store.load(control.camera.mac)[homes[0].index].name == "home"
    assert ctl.ptz_health(control.camera.mac)["home_preset"] is True


def test_auto_home_never_replaces_an_existing_home_or_runs_when_disabled() -> None:
    ctl, control, _, ptz_session, ptz_wire = _ptz_pair()
    control.camera.presets = {4: Preset(4, "Home", Position(1, 2, 3, 4))}
    ctl._dispatch(ptz_session, _position_reply(ptz_session))
    assert control.camera.presets[4].position == Position(1, 2, 3, 4)
    assert not [m for m in ptz_wire.sent() if m.payload.get("action") == "config"]

    ctl2, control2, _, ptz_session2, _ = _ptz_pair(auto_home=False)
    ctl2._dispatch(ptz_session2, _position_reply(ptz_session2))
    assert control2.camera.presets == {}


def test_ptz_health_reports_a_healthy_channel() -> None:
    ctl, control, _, ptz_session, _ = _ptz_pair(auto_home=False)
    ctl._dispatch(ptz_session, _position_reply(ptz_session))
    health = ctl.ptz_health(control.camera.mac)
    assert health["healthy"] is True
    assert health["state"] == "IDLE"
    assert health["channel"] == "up"
    assert isinstance(health["last_motor_reply_age_sec"], float)
    assert health["recovery"] == {
        "active": False, "attempts": 0, "max_attempts": controller.PTZ_RECOVERY_MAX_ATTEMPTS,
        "waiting_for_callback": False, "exhausted": False,
    }
