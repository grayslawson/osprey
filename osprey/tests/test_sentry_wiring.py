"""Wiring the idle patrol into the controller: sources, activity, and config.

These are the pieces that make a patrol safe next to a real autotracker: the
``PATROL`` arbitration source, the record of when an autotracker last drove a
camera, and the strict preset move that refuses to swallow a dropped command.
"""

from __future__ import annotations

import argparse

import pytest

import arbitration
import config
import main
from arbitration import AUTO, MANUAL, PATROL, Arbiter
from model import Position
from test_adoption_flow import _ptz_harness

MAC = "AABBCCDDEEFF"


def _as(kind: str, call):  # type: ignore[no-untyped-def]
    token = arbitration.source.set(kind)
    try:
        return call()
    finally:
        arbitration.source.reset(token)


# ------------------------------------------------------------------ the source


def test_patrol_is_a_source_of_its_own_that_yields_to_a_person() -> None:
    arbiter = Arbiter(30)
    assert arbiter.admit(MAC, PATROL) == (True, False)
    arbiter.admit(MAC, MANUAL)
    # A manual hold stops a patrol exactly as it stops an autotracker.
    assert arbiter.admit(MAC, PATROL) == (False, False)
    assert arbiter.dropped == 1


def test_a_patrol_is_not_an_autotracker() -> None:
    assert str(PATROL) != str(AUTO)
    assert arbitration.source.get() is None


# ------------------------------------------------------- controller behaviour


def test_the_controller_records_when_an_autotracker_drove_the_camera() -> None:
    ctl, _session, _wire = _ptz_harness()
    assert ctl.auto_active(MAC, 10) is False
    _as(AUTO, lambda: ctl.move(MAC, Position(pan=1000, tilt=9000)))
    assert ctl.auto_active(MAC, 10) is True
    assert ctl.auto_active(MAC, 0) is False


def test_a_patrol_move_is_not_recorded_as_autotracking() -> None:
    ctl, _session, _wire = _ptz_harness()
    _as(PATROL, lambda: ctl.move(MAC, Position(pan=1000, tilt=9000)))
    assert ctl.auto_active(MAC, 10) is False


def test_activity_is_tracked_per_camera() -> None:
    ctl, _session, _wire = _ptz_harness()
    _as(AUTO, lambda: ctl.move(MAC, Position(pan=1000, tilt=9000)))
    assert ctl.auto_active(MAC, 10) is True
    assert ctl.auto_active("OTHERCAMERA", 10) is False


def test_a_strict_preset_move_reports_a_refusal_a_lenient_one_does_not() -> None:
    ctl, _session, wire = _ptz_harness()
    _as(MANUAL, lambda: ctl.move(MAC, Position(pan=1000, tilt=9000)))
    sent = len(wire.sent())
    # During the hold the command is dropped. An autotracker is told "fine",
    # because a fault would make it log errors or back off.
    assert _as(AUTO, lambda: ctl.goto_preset(MAC, 1)) is True
    # A patrol is told the truth, so it does not consume a point of its tour.
    assert _as(PATROL, lambda: ctl.goto_preset(MAC, 1, strict=True)) is False
    assert len(wire.sent()) == sent


def test_a_strict_preset_move_still_succeeds_outside_a_manual_hold() -> None:
    ctl, _session, _wire = _ptz_harness()
    assert _as(PATROL, lambda: ctl.goto_preset(MAC, 1, strict=True)) is True


# --------------------------------------------------------------------- config


def test_validate_sentry_defaults_to_a_disabled_patrol() -> None:
    parsed = config.validate_sentry(None)
    assert parsed["enabled"] is False
    assert parsed["cameras"] == {}
    assert parsed["interval_sec"] == 45
    assert parsed["speed"] == 1000


def test_validate_sentry_lets_a_camera_override_the_shared_defaults() -> None:
    parsed = config.validate_sentry({
        "enabled": True,
        "interval_sec": 30,
        "speed": 800,
        "cameras": {
            "aa:bb:cc:dd:ee:ff": {"points": [{"name": "gate"}]},
            "001122334455": {"interval_sec": 60, "speed": 400},
        },
    })
    first = parsed["cameras"]["AABBCCDDEEFF"]
    assert first["enabled"] is True
    assert first["interval_sec"] == 30
    assert first["speed"] == 800
    assert first["points"] == [{"name": "gate", "dwell_sec": None}]
    second = parsed["cameras"]["001122334455"]
    assert second["interval_sec"] == 60
    assert second["speed"] == 400
    assert second["points"] == []


def test_validate_sentry_rejects_two_spellings_of_the_same_camera() -> None:
    with pytest.raises(ValueError):
        config.validate_sentry({
            "cameras": {
                "aa:bb:cc:dd:ee:ff": {},
                "AABBCCDDEEFF": {},
            },
        })


@pytest.mark.parametrize("raw", [
    {"enabled": 1},
    {"interval_sec": 0},
    {"interval_sec": "nope"},
    {"speed": 0},
    {"speed": "fast"},
    {"tracking_hold_sec": 0},
    {"cameras": []},
    {"cameras": {"not-a-mac": {}}},
    {"cameras": {"AABBCCDDEEFF": "nope"}},
    {"cameras": {"AABBCCDDEEFF": {"unknown": 1}}},
    {"cameras": {"AABBCCDDEEFF": {"points": [{"name": "a"}, {"name": "A"}]}}},
    {"cameras": {"AABBCCDDEEFF": {"points": [{"name": "a", "bogus": 1}]}}},
    {"cameras": {"AABBCCDDEEFF": {"points": [{"dwell_sec": 3}]}}},
])
def test_validate_sentry_rejects_malformed_settings(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        config.validate_sentry(raw)


def test_validate_sentry_rejects_unknown_top_level_keys() -> None:
    with pytest.raises(ValueError):
        config.validate_sentry({"bogus": 1})


def test_the_runtime_validator_checks_the_sentry_block() -> None:
    merged = config.merged({"host": "192.0.2.10"})
    config.validate_runtime(merged)
    merged["sentry"] = {"cameras": {"AABBCCDDEEFF": {"points": [{"name": ""}]}}}
    with pytest.raises(ValueError):
        config.validate_runtime(merged)


def test_resolve_options_carries_the_sentry_block_through() -> None:
    args = argparse.Namespace(
        config=None, host="192.0.2.10", bind=None, name=None, cert=None,
        no_announce=False, tracks=None, setup=False, verbose=False, dump=None,
        control_port=None, ingest_port=None, snapshot_port=None, rtsp_port=None,
        onvif_port=None, onvif_read_only_port=None, discovery_port=None,
        expected_camera_ip=None, expected_camera_mac=None,
    )
    options = main.resolve_options(args)
    assert options.sentry["enabled"] is False
    assert options.sentry["cameras"] == {}
