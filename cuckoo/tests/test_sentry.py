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
