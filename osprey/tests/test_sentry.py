from __future__ import annotations

import threading
from typing import Any

import pytest

from sentry import PatrolPoint, SentryPatrol


def make_patrol(**kwargs: Any) -> tuple[SentryPatrol, list[str], dict[str, bool]]:
    calls: list[str] = []
    flags = {"tracking": False, "idle": True}

    def goto(name: str) -> bool:
        calls.append(name)
        return True

    patrol = SentryPatrol(
        goto_preset=goto,
        tracking_active=lambda: flags["tracking"],
        camera_idle=lambda: flags["idle"],
        interval_sec=10,
        **kwargs,
    )
    patrol.configure([PatrolPoint("gate"), PatrolPoint("driveway", dwell_sec=20)])
    return patrol, calls, flags


def test_patrol_rotates_named_points_and_uses_dwell() -> None:
    patrol, calls, _ = make_patrol()
    patrol.enable(now=100)
    assert not patrol.step(now=109)
    assert patrol.step(now=110)
    assert calls == ["gate"]
    assert patrol.snapshot().next_move_at == 120
    assert patrol.step(now=120)
    assert calls == ["gate", "driveway"]
    assert patrol.snapshot().next_move_at == 140


def test_tracking_suspends_without_consuming_point() -> None:
    patrol, calls, flags = make_patrol()
    patrol.enable(now=0)
    flags["tracking"] = True
    assert not patrol.step(now=10)
    assert calls == []
    flags["tracking"] = False
    assert patrol.step(now=11)
    assert calls == ["gate"]


def test_rechecks_inside_shared_lease_and_recovers_when_busy() -> None:
    patrol, calls, flags = make_patrol()
    race_lease = _RaceLease(flags)
    patrol.movement_lease = lambda: race_lease
    patrol.enable(now=0)
    assert not patrol.step(now=10)
    assert calls == []
    flags["idle"] = True
    assert patrol.step(now=11)
    assert calls == ["gate"]


class _RaceLease:
    def __init__(self, flags: dict[str, bool]) -> None:
        self.flags = flags
        self.first = True

    def __enter__(self) -> None:
        if self.first:
            self.first = False
            self.flags["idle"] = False

    def __exit__(self, *_args: object) -> None:
        self.flags["idle"] = True


def test_lease_contention_is_deferred() -> None:
    lock = threading.Lock()
    lock.acquire()
    patrol, calls, _ = make_patrol(movement_lease=lambda: _TryLock(lock))
    patrol.enable(now=0)
    assert not patrol.step(now=10)
    assert calls == []
    lock.release()
    assert patrol.step(now=11)


class _TryLock:
    def __init__(self, lock: threading.Lock) -> None:
        self.lock = lock

    def __enter__(self) -> None:
        if not self.lock.acquire(blocking=False):
            raise BlockingIOError

    def __exit__(self, *_args: object) -> None:
        self.lock.release()


def test_errors_are_visible_and_scheduler_continues() -> None:
    attempts = 0

    def goto(_name: str) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("camera offline")
        return True

    patrol = SentryPatrol(goto, lambda: False, lambda: True, interval_sec=10)
    patrol.configure([PatrolPoint("yard")])
    patrol.enable(now=0)
    assert not patrol.step(now=10)
    assert patrol.snapshot().state == "error"
    assert "offline" in (patrol.snapshot().last_error or "")
    assert patrol.step(now=11)


def test_configuration_rejects_empty_duplicate_and_short_dwell() -> None:
    patrol = SentryPatrol(lambda _n: True, lambda: False, lambda: True)
    with pytest.raises(ValueError):
        patrol.configure([PatrolPoint(" ")])
    with pytest.raises(ValueError):
        patrol.configure([PatrolPoint("Gate"), PatrolPoint("gate")])
    with pytest.raises(ValueError):
        patrol.configure([PatrolPoint("gate", dwell_sec=0)])


def night_patrol(missing: set[str] | None = None, hold: float = 120.0) -> tuple[SentryPatrol, list[str], dict[str, bool]]:
    calls: list[str] = []
    scene = {"night": False}

    def goto(name: str) -> bool:
        calls.append(name)
        return name not in (missing or set())

    patrol = SentryPatrol(
        goto_preset=goto, tracking_active=lambda: False, camera_idle=lambda: True,
        interval_sec=10, is_night=lambda: scene["night"], night_hold_sec=hold,
    )
    patrol.configure([PatrolPoint("porch", night_preset="porch-night"), PatrolPoint("home")])
    return patrol, calls, scene


def test_night_preset_replaces_the_point_only_at_night() -> None:
    patrol, calls, scene = night_patrol(hold=0)
    patrol.enable(now=0)
    assert patrol.step(now=10) and patrol.step(now=20)
    assert calls == ["porch", "home"]
    scene["night"] = True
    assert patrol.step(now=30) and patrol.step(now=40)
    assert calls[2:] == ["porch-night", "home"]  # points without a night preset are unchanged
    assert patrol.snapshot().point == "home"


def test_missing_night_preset_falls_back_to_the_normal_one() -> None:
    patrol, calls, scene = night_patrol(missing={"porch-night"}, hold=0)
    scene["night"] = True
    patrol.enable(now=0)
    assert patrol.step(now=10)
    assert calls == ["porch-night", "porch"]
    assert patrol.snapshot().point == "porch"


def test_scene_flicker_does_not_switch_views_until_it_holds() -> None:
    patrol, calls, scene = night_patrol(hold=120)
    patrol.enable(now=0)
    assert patrol.step(now=10)  # day, latched
    scene["night"] = True
    assert patrol.step(now=20) and patrol.step(now=30)  # night seen, not held yet
    scene["night"] = False
    assert patrol.step(now=40)  # back to day: the pending change is dropped
    scene["night"] = True
    assert patrol.step(now=50)
    assert patrol.step(now=200)  # night has now held for 150 s
    assert patrol.step(now=210) and patrol.step(now=220)
    assert calls == ["porch", "home", "porch", "home", "porch", "home", "porch-night", "home"]
