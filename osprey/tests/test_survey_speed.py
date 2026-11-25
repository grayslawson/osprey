"""``survey.speed`` slows the sweep down instead of being accepted and ignored.

The key was parsed and range checked but never read: the sweep always ran at
the camera's ceiling and the time estimate always assumed it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import ptz
from assist import survey_plan as sp
from stub_models import StubModels
from survey_world import decisions_reply
from test_survey import SRig


@pytest.fixture
def models() -> Iterator[StubModels]:
    with StubModels(decisions_reply=decisions_reply) as stub:
        yield stub


def _ceiling(rig: SRig) -> int:
    return int(rig.world.service.backend.max_speed)


def test_the_move_carries_the_speed_the_client_asked_for(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        rig.world.client.move_absolute(0.1, 0.0, 0.0, speed=0.5)
        assert rig.world.move_speeds == [ptz.speed_from_normalised(0.5, _ceiling(rig))]
    finally:
        rig.close()


def test_a_move_without_a_speed_runs_at_the_ceiling(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        rig.world.client.move_absolute(0.1, 0.0, 0.0)
        assert rig.world.move_speeds == [_ceiling(rig)]
    finally:
        rig.close()


def test_the_survey_moves_at_the_configured_speed(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models, survey={"speed": 0.5})
    try:
        rig.run()
        assert rig.world.move_speeds, "the survey should have moved the camera"
        assert all(speed == ptz.speed_from_normalised(0.5, _ceiling(rig))
                   for speed in rig.world.move_speeds)
        assert all(speed < _ceiling(rig) for speed in rig.world.move_speeds)
    finally:
        rig.close()


def test_the_survey_defaults_to_the_ceiling(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        rig.run()
        assert rig.world.move_speeds, "the survey should have moved the camera"
        assert all(speed == _ceiling(rig) for speed in rig.world.move_speeds)
    finally:
        rig.close()


def test_the_estimate_gets_longer_as_the_sweep_gets_slower() -> None:
    geo = sp.Geometry(pan_deg=90.0, tilt_deg=45.0, wide=(90.0, 60.0), tele=(30.0, 20.0))
    cells = sp.plan(geo, 0.25, 0.0)
    fast = sp.estimate_sec(geo, cells, 0, sp.Timing(deg_per_sec=sp.BASE_DEG_PER_SEC))
    slow = sp.estimate_sec(geo, cells, 0, sp.Timing(deg_per_sec=sp.BASE_DEG_PER_SEC * 0.5))
    assert slow["sweep_sec"] > fast["sweep_sec"]
    assert slow["total_sec"] > fast["total_sec"]


def test_the_config_range_is_enforced() -> None:
    from assist import config as acfg

    with pytest.raises(ValueError):
        acfg.parse({"survey": {"speed": 2.0}})
