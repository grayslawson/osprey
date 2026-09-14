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
    calls: list[str] = []

    def goto(name: str) -> bool:
        calls.append(name)
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
    assert calls == ["Gate"]


def test_gate_can_be_shared_across_runtime_and_other_producer() -> None:
    gate = MovementGate()
    config = SentryConfig.from_mapping({"interval_sec": 1, "points": [{"name": "Gate"}]})
    calls: list[str] = []

    def goto(name: str) -> bool:
        calls.append(name)
        return True

    runtime = SentryRuntime(config, goto, lambda: False, lambda: True, gate)
    with gate.lease("autotracking"):
        runtime.patrol.enable(now=0)
        assert runtime.patrol.step(now=1) is False
    assert calls == []
