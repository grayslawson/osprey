from __future__ import annotations

import pytest

from sentry_runtime import MovementGate, SentryConfig, SentryRuntime


def test_config_validates_and_round_trips() -> None:
    config = SentryConfig.from_mapping({
        "enabled": True,
        "interval_sec": 12,
        "points": [{"name": "Gate", "dwell_sec": 5}],
    })
    assert config.enabled is True
    assert config.as_dict()["points"] == [{"name": "Gate", "dwell_sec": 5.0}]


@pytest.mark.parametrize("raw", [
    {"enabled": 1},
    {"interval_sec": 0},
    {"interval_sec": "nope"},
    {"points": "Gate"},
    {"points": [{"dwell_sec": 3}]},
    {"speed": 0},
    {"speed": "fast"},
    {"speed": True},
    {"tracking_hold_sec": 0},
    {"tracking_hold_sec": "nope"},
])
def test_config_rejects_unsafe_shapes(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SentryConfig.from_mapping(raw)


def test_gate_is_nonblocking_and_releases_after_exception() -> None:
    gate = MovementGate()
    entered = gate.lease("manual")
    entered.__enter__()
    try:
        with pytest.raises(BlockingIOError):
            with gate.lease("sentry"):
                pass
        assert gate.snapshot()["rejected"] == 1
    finally:
        entered.__exit__(None, None, None)
    assert gate.snapshot()["busy"] is False
    with gate.lease("sentry"):
        assert gate.snapshot()["owner"] == "sentry"


def test_runtime_does_not_move_until_interval_and_exposes_status() -> None:
    calls: list[tuple[str, int]] = []

    def goto(name: str, speed: int) -> bool:
        calls.append((name, speed))
        return True

    runtime = SentryRuntime(
        SentryConfig.from_mapping({"interval_sec": 10, "points": [{"name": "Gate"}]}),
        goto,
        lambda: False,
        lambda: True,
    )
    assert calls == []
    state = runtime.snapshot()["state"]
    assert isinstance(state, dict)
    assert state["enabled"] is False
    runtime.patrol.enable(now=0)
    assert runtime.patrol.step(now=9) is False
    assert runtime.patrol.step(now=10) is True
    assert calls == [("Gate", 1000)]


def test_runtime_passes_the_configured_speed_to_the_move() -> None:
    calls: list[tuple[str, int]] = []

    def goto(name: str, speed: int) -> bool:
        calls.append((name, speed))
        return True

    runtime = SentryRuntime(
        SentryConfig.from_mapping({"speed": 250, "points": [{"name": "Gate"}]}),
        goto,
        lambda: False,
        lambda: True,
    )
    runtime.patrol.enable(now=0)
    assert runtime.patrol.step(now=0) is False
    runtime.patrol._next_move_at = 0.0  # the first move waits out the interval
    assert runtime.patrol.step(now=1) is True
    assert calls == [("Gate", 250)]


def test_gate_can_be_shared_across_runtime_and_other_producer() -> None:
    gate = MovementGate()
    config = SentryConfig.from_mapping({"interval_sec": 1, "points": [{"name": "Gate"}]})
    calls: list[tuple[str, int]] = []

    def goto(name: str, speed: int) -> bool:
        calls.append((name, speed))
        return True

    runtime = SentryRuntime(config, goto, lambda: False, lambda: True, gate)
    with gate.lease("autotracking"):
        runtime.patrol.enable(now=0)
        assert runtime.patrol.step(now=1) is False
    assert calls == []


def test_gate_keys_are_per_camera_so_one_patrol_cannot_block_another() -> None:
    gate = MovementGate()
    with gate.lease("sentry", "camera-a"):
        assert gate.snapshot()["owner"] == "sentry"
        # A different camera is not blocked by the first one's lease.
        with gate.lease("sentry", "camera-b"):
            assert _camera_entry(gate, "camera-b")["owner"] == "sentry"
        assert _camera_entry(gate, "camera-a")["busy"] is True
    assert gate.snapshot()["busy"] is False


def _camera_entry(gate: MovementGate, key: str) -> dict[str, object]:
    cameras = gate.snapshot()["cameras"]
    assert isinstance(cameras, dict)
    entry = cameras[key]
    assert isinstance(entry, dict)
    return entry


def test_gate_rejects_a_second_holder_of_the_same_camera() -> None:
    gate = MovementGate()
    with gate.lease("sentry", "camera-a"):
        with pytest.raises(BlockingIOError):
            with gate.lease("manual", "camera-a"):
                pass
    assert gate.snapshot()["rejected"] == 1
