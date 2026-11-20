"""The multi-camera idle patrol: one tour per camera, shared movement gate.

The manager is the piece that turns the single-tour engine into something a
controller can run for several cameras at once.  These tests drive it with
fakes so no camera, no network and no sleeping is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from sentry_manager import SentryManager, SentryStore
from sentry_runtime import MovementGate, SentryConfig


class World:
    """A fake deployment: what moved, what is tracking, what is busy."""

    def __init__(self) -> None:
        self.moves: list[tuple[str, str, int]] = []
        self.tracking: set[str] = set()
        self.busy: set[str] = set()
        self.refused: set[str] = set()
        self.holds: dict[str, int] = {}

    def goto(self, camera: str, name: str, speed: int) -> bool:
        if camera in self.refused:
            return False
        self.moves.append((camera, name, speed))
        return True

    def tracking_active(self, camera: str, hold_sec: float) -> bool:
        return camera in self.tracking

    def idle(self, camera: str) -> bool:
        return camera not in self.busy

    def presets(self, camera: str) -> list[str]:
        return ["driveway", "gate", "home"]


def make_manager(world: World) -> SentryManager:
    return SentryManager(
        goto_preset=world.goto,
        tracking_active=world.tracking_active,
        camera_idle=world.idle,
        gate=MovementGate(),
        preset_names=world.presets,
    )


def state_of(body: dict[str, object]) -> dict[str, object]:
    """The live-state half of a camera body, narrowed for the type checker."""
    state = body["state"]
    assert isinstance(state, dict)
    return state


def config_for(camera: str, points: list[str], **kwargs: Any) -> SentryConfig:
    raw: dict[str, Any] = {
        "enabled": True,
        "interval_sec": 10,
        "points": [{"name": name} for name in points],
    }
    raw.update(kwargs)
    return SentryConfig.from_mapping(raw)


def test_manager_runs_independent_patrols_per_camera() -> None:
    world = World()
    manager = make_manager(world)
    manager.configure("AAA", config_for("AAA", ["driveway"], interval_sec=10))
    manager.configure("BBB", config_for("BBB", ["gate", "home"], interval_sec=10, speed=250))
    try:
        # Each camera starts at the first point of its own tour.
        manager._runtimes["AAA"].patrol.enable(now=0)
        manager._runtimes["BBB"].patrol.enable(now=0)
        assert manager._runtimes["AAA"].patrol.step(now=10) is True
        assert manager._runtimes["BBB"].patrol.step(now=10) is True
        assert world.moves == [("AAA", "driveway", 1000), ("BBB", "gate", 250)]
        # The tours rotate independently.
        manager._runtimes["AAA"].patrol.step(now=20)
        manager._runtimes["BBB"].patrol.step(now=20)
        assert world.moves[-2:] == [("AAA", "driveway", 1000), ("BBB", "home", 250)]
    finally:
        manager.stop()


def test_sentry_patrol_defers_to_autotracking() -> None:
    world = World()
    manager = make_manager(world)
    manager.configure("AAA", config_for("AAA", ["driveway"]))
    try:
        patrol = manager._runtimes["AAA"].patrol
        patrol.enable(now=0)
        world.tracking.add("AAA")
        assert patrol.step(now=10) is False
        assert world.moves == []
        assert patrol.snapshot().state == "tracking"
        # The point was not consumed: the tour resumes where it left off.
        world.tracking.discard("AAA")
        assert patrol.step(now=11) is True
        assert world.moves == [("AAA", "driveway", 1000)]
    finally:
        manager.stop()


def test_sentry_patrol_defers_to_manual_control() -> None:
    world = World()
    manager = make_manager(world)
    manager.configure("AAA", config_for("AAA", ["driveway"]))
    try:
        patrol = manager._runtimes["AAA"].patrol
        patrol.enable(now=0)
        world.busy.add("AAA")
        assert patrol.step(now=10) is False
        assert world.moves == []
        assert patrol.snapshot().state == "camera_busy"
        world.busy.discard("AAA")
        assert patrol.step(now=11) is True
        assert world.moves == [("AAA", "driveway", 1000)]
    finally:
        manager.stop()


def test_unknown_point_is_reported_as_an_error() -> None:
    world = World()
    manager = make_manager(world)
    manager.configure("AAA", config_for("AAA", ["nowhere"]))
    try:
        patrol = manager._runtimes["AAA"].patrol
        patrol.enable(now=0)
        world.refused.add("AAA")
        assert patrol.step(now=10) is False
        snapshot = patrol.snapshot()
        assert snapshot.state == "error"
        assert "refused" in (snapshot.last_error or "")
    finally:
        manager.stop()


def test_patrol_moves_are_serialised_by_the_lease() -> None:
    world = World()
    gate = MovementGate()
    manager = SentryManager(
        goto_preset=world.goto,
        tracking_active=world.tracking_active,
        camera_idle=world.idle,
        gate=gate,
    )
    manager.configure("AAA", config_for("AAA", ["driveway"]))
    try:
        patrol = manager._runtimes["AAA"].patrol
        patrol.enable(now=0)
        with gate.lease("console", "AAA"):
            assert patrol.step(now=10) is False
        assert world.moves == []
        # The point was not consumed, so the tour continues.
        assert patrol.step(now=11) is True
        assert world.moves == [("AAA", "driveway", 1000)]
    finally:
        manager.stop()


def test_enable_and_disable_start_and_stop_the_patrol() -> None:
    world = World()
    manager = make_manager(world)
    manager.configure("AAA", config_for("AAA", ["driveway"], enabled=False))
    try:
        assert manager._runtimes == {}
        assert state_of(manager.snapshot("AAA"))["state"] == "disabled"
        manager.enable("AAA")
        assert "AAA" in manager._runtimes
        assert state_of(manager.snapshot("AAA"))["enabled"] is True
        manager.disable("AAA")
        assert "AAA" not in manager._runtimes
        assert state_of(manager.snapshot("AAA"))["state"] == "disabled"
        # The points survive a disable so the patrol can be started again.
        assert manager.config("AAA") is not None
        assert manager.config("AAA").points  # type: ignore[union-attr]
    finally:
        manager.stop()


def test_remove_forgets_the_patrol_entirely() -> None:
    world = World()
    manager = make_manager(world)
    manager.configure("AAA", config_for("AAA", ["driveway"]))
    manager.remove("AAA")
    assert manager.config("AAA") is None
    assert manager.cameras() == ()


def test_duplicate_and_empty_point_names_are_rejected() -> None:
    world = World()
    manager = make_manager(world)
    with pytest.raises(ValueError):
        manager.configure("AAA", SentryConfig.from_mapping({
            "points": [{"name": "gate"}, {"name": "GATE"}],
        }))
    with pytest.raises(ValueError):
        manager.configure("AAA", SentryConfig.from_mapping({"points": [{"name": "  "}]}))


def test_a_rejected_configuration_leaves_the_running_patrol_alone() -> None:
    world = World()
    manager = make_manager(world)
    manager.configure("AAA", config_for("AAA", ["driveway"]))
    try:
        with pytest.raises(ValueError):
            manager.configure("AAA", SentryConfig.from_mapping({
                "enabled": True, "points": [{"name": "gate"}, {"name": "gate"}],
            }))
        # The original patrol is still the one that is running.
        assert manager.config("AAA").points[0].name == "driveway"  # type: ignore[union-attr]
        assert "AAA" in manager._runtimes
    finally:
        manager.stop()


def test_status_reports_the_config_the_state_and_the_presets() -> None:
    world = World()
    manager = make_manager(world)
    manager.configure("AAA", config_for("AAA", ["driveway"], speed=500))
    try:
        body = manager.snapshot("AAA")
        assert body["camera"] == "AAA"
        config = body["config"]
        assert isinstance(config, dict)
        assert config["speed"] == 500
        assert config["interval_sec"] == 10
        assert state_of(body)["points"] == ["driveway"]
        assert body["presets"] == ["driveway", "gate", "home"]
        movement = body["movement"]
        assert isinstance(movement, dict)
        assert movement["busy"] is False
        overview = manager.snapshot()
        cameras = overview["cameras"]
        assert isinstance(cameras, dict)
        assert "AAA" in cameras
    finally:
        manager.stop()


# --------------------------------------------------------------- persistence


def test_the_store_round_trips_per_camera_settings(tmp_path: Any) -> None:
    store = SentryStore(tmp_path / "sentry.json")
    assert store.load() == {}
    assert store.save({"AAA": {"enabled": True, "points": [{"name": "gate"}]}}) is True
    loaded = store.load()
    assert loaded["AAA"]["enabled"] is True
    assert loaded["AAA"]["points"] == [{"name": "gate"}]


def test_an_unreadable_store_is_ignored_rather_than_fatal(tmp_path: Any) -> None:
    path = tmp_path / "sentry.json"
    path.write_text("{not json", encoding="utf-8")
    assert SentryStore(path).load() == {}


def test_restore_prefers_the_stored_settings_over_the_baseline(tmp_path: Any) -> None:
    world = World()
    store = SentryStore(tmp_path / "sentry.json")
    store.save({"AAA": {"enabled": True, "interval_sec": 10, "speed": 400,
                        "tracking_hold_sec": 15, "points": [{"name": "gate"}]}})
    manager = make_manager(world)
    manager.store = store
    manager.restore({
        "AAA": {"enabled": False, "interval_sec": 90, "speed": 1000,
                "tracking_hold_sec": 15, "points": [{"name": "driveway"}]},
    })
    try:
        config = manager.config("AAA")
        assert config is not None
        assert config.interval_sec == 10
        assert config.speed == 400
        assert [point.name for point in config.points] == ["gate"]
        assert config.enabled is True
    finally:
        manager.stop()


def test_restore_uses_the_baseline_for_a_camera_the_store_does_not_mention(tmp_path: Any) -> None:
    world = World()
    store = SentryStore(tmp_path / "sentry.json")
    manager = make_manager(world)
    manager.store = store
    manager.restore({
        "AAA": {"enabled": True, "interval_sec": 45, "speed": 1000,
                "tracking_hold_sec": 15, "points": [{"name": "driveway"}]},
    })
    try:
        config = manager.config("AAA")
        assert config is not None
        assert [point.name for point in config.points] == ["driveway"]
    finally:
        manager.stop()


def test_a_removed_patrol_does_not_come_back_after_a_restart(tmp_path: Any) -> None:
    world = World()
    store = SentryStore(tmp_path / "sentry.json")
    manager = make_manager(world)
    manager.store = store
    manager.configure("AAA", config_for("AAA", ["driveway"]))
    manager.remove("AAA")
    assert manager.config("AAA") is None

    # A restart reads the baseline from the config file and the tombstone
    # from the state file; the tombstone wins.
    restarted = make_manager(world)
    restarted.store = store
    restarted.restore({
        "AAA": {"enabled": True, "interval_sec": 45, "speed": 1000,
                "tracking_hold_sec": 15, "points": [{"name": "driveway"}]},
    })
    try:
        assert restarted.config("AAA") is None
        assert "AAA" not in restarted._runtimes
    finally:
        restarted.stop()


def test_a_configured_patrol_survives_a_restart(tmp_path: Any) -> None:
    world = World()
    store = SentryStore(tmp_path / "sentry.json")
    manager = make_manager(world)
    manager.store = store
    manager.configure("AAA", config_for("AAA", ["driveway", "gate"]))
    manager.stop()

    restarted = make_manager(world)
    restarted.store = store
    restarted.restore({})
    try:
        config = restarted.config("AAA")
        assert config is not None
        assert [point.name for point in config.points] == ["driveway", "gate"]
        assert "AAA" in restarted._runtimes
    finally:
        restarted.stop()
