"""Assist against a simulated camera: the whole loop through the real Osprey web API."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import assist.config as acfg
from assist.decide import Decider
from assist.engine import Engine, Refused
from assist.store import Store
from assist_world import ISP_ALLOWED, Clock, World


def make_cfg(tmp: Path, **over: Any) -> acfg.AssistConfig:
    raw: dict[str, Any] = {"enabled": True, "storage": {"dir": str(tmp / "state")}, "capabilities": {"quality": {
        "mode": "suggest", "settle_sec": 1, "isp_settle_sec": 1, "verify_after_sec": [60, 600]}}}
    for key, value in over.items():
        if key == "quality":
            raw["capabilities"]["quality"].update(value)
        else:
            raw[key] = value
    return acfg.parse(raw)


class Rig:
    def __init__(self, tmp: Path, *, mode: str = "suggest", world: dict[str, Any] | None = None, **over: Any) -> None:
        self.clock = Clock()
        self.world = World(tmp, self.clock, **(world or {}))
        self.cfg = make_cfg(tmp, quality={"mode": mode}, **over)
        self.store = Store(self.cfg.state_dir, clock=self.clock)
        self.engine = Engine(self.cfg, self.store, {"default": self.world.client}, Decider(self.cfg.tasks, {}, self.store.log_call),
                             clock=self.clock, sleep=self.clock.sleep)
        self.cam = self.world.camera

    def warm(self, n: int = 10) -> None:
        """Learn a baseline from healthy samples of the current view."""
        glare, self.world.glare = self.world.glare, None
        exposure = self.world.exposure
        self.world.exposure = 1.0
        for _ in range(n):
            self.engine.check("default", "manual")
            self.clock.now += 700
        self.world.glare, self.world.exposure = glare, exposure

    def close(self) -> None:
        self.world.close()
        self.store.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path)
    try:
        yield r
    finally:
        r.close()


def goto(rig: Any, name: str) -> None:
    rig.world._goto_name(name)


def test_night_glare_becomes_a_night_view_suggestion(rig: Rig) -> None:
    goto(rig, "BackPorch")
    rig.warm()
    base = rig.cam.motion.position
    found = None
    for _ in range(2):  # the two-sample rule
        found = rig.engine.check("default", "arrival") or found
        rig.clock.now += 700
    assert found is not None
    row = rig.store.decision(found)
    assert row is not None
    assert row["status"] == "pending" and row["problem"] == "glare" and row["structural"]
    assert row["change"].startswith("New night view BackPorchNight")
    assert row["before_frame"] and row["after_frame"] and row["warning"]
    assert rig.cam.motion.position == base  # the head went back where it was
    assert rig.world.manager.hold_remaining(rig.cam.mac) == 0  # and the patrol hold was released
    assert "BackPorchNight" not in rig.world.preset_names()  # nothing is saved before approval
    after = rig.store.frame(row["after_frame"])
    assert after is not None and rig.store.frame(row["before_frame"]) is not None
    assert row["after_metrics"]["glare"] <= row["metrics"]["glare"] * 0.5


def test_approving_the_night_view_saves_the_preset_and_sets_the_patrol(rig: Rig) -> None:
    goto(rig, "BackPorch")
    rig.warm()
    ids = [rig.engine.check("default", "arrival") for _ in range(2)]
    decision = next(i for i in ids if i)
    out = rig.engine.approve(decision, "console")
    assert out["status"] == "applied"
    assert "BackPorchNight" in rig.world.preset_names()
    points = rig.world.manager.config(rig.cam.mac).points  # type: ignore[union-attr]
    assert {p.name: p.night_preset for p in points}["BackPorch"] == "BackPorchNight"
    # at night the patrol's night view is judged on its next visit
    goto(rig, "BackPorchNight")
    rig.clock.now += 120
    rig.engine.verify_visit("default", "BackPorchNight", "NIGHT")
    assert rig.store.decision(decision)["status"] == "applied"  # type: ignore[index]  # one more check is owed
    rig.clock.now += 700
    rig.engine.verify_visit("default", "BackPorchNight", "NIGHT")
    assert rig.store.decision(decision)["status"] == "kept"  # type: ignore[index]


def test_undoing_a_night_view_clears_the_patrol_setting(rig: Rig) -> None:
    goto(rig, "BackPorch")
    rig.warm()
    decision = next(i for i in [rig.engine.check("default", "arrival") for _ in range(2)] if i)
    rig.engine.approve(decision, "console")
    out = rig.engine.undo(decision, "console")
    assert out["status"] == "undone"
    points = rig.world.manager.config(rig.cam.mac).points  # type: ignore[union-attr]
    assert all(p.night_preset is None for p in points)


def test_a_rejection_teaches_and_five_mute(rig: Rig) -> None:
    goto(rig, "BackPorch")
    rig.warm()
    decision = next(i for i in [rig.engine.check("default", "arrival") for _ in range(2)] if i)
    rig.engine.reject(decision, "ha", "not_a_problem")
    assert float(rig.store.get("scale:default:BackPorch:glare")) > 1.0
    with pytest.raises(Refused):
        rig.engine.approve(decision, "console")  # it is not pending any more


def test_overexposure_in_act_mode_applies_then_keeps_with_a_profile(tmp_path: Path) -> None:
    rig = Rig(tmp_path, mode="act", world={"night": False, "glare": None})
    try:
        goto(rig, "Gate")
        rig.warm()
        rig.world.exposure = 1.5
        ids = [rig.engine.check("default", "arrival") for _ in range(3)]
        decision = next(i for i in ids if i)
        row = rig.store.decision(decision)
        assert row is not None and row["status"] == "applied" and row["problem"] in ("over", "glare")
        assert rig.world.isp["aeTargetPercent"] == 45
        rig.clock.now += 61
        rig.engine.verify_visit("default", "Gate", "DAY")
        rig.clock.now += 600
        rig.engine.verify_visit("default", "Gate", "DAY")
        assert rig.store.decision(decision)["status"] == "kept"  # type: ignore[index]
        assert rig.store.profile("default", "Gate", "DAY") == {"aeTargetPercent": 45}
        # arriving at another preset puts the setting back home; coming back re-applies it
        rig.engine._apply_profile(rig.engine.cams["default"], "BackPorch", "DAY")
        assert rig.world.isp["aeTargetPercent"] == 50
        rig.engine._apply_profile(rig.engine.cams["default"], "Gate", "DAY")
        assert rig.world.isp["aeTargetPercent"] == 45
    finally:
        rig.close()


def test_a_change_that_does_not_help_is_reverted_and_cooled_down(tmp_path: Path) -> None:
    rig = Rig(tmp_path, mode="act", world={"night": False, "glare": None})
    try:
        goto(rig, "Gate")
        rig.warm()
        rig.world.exposure = 1.5
        decision = next(i for i in [rig.engine.check("default", "arrival") for _ in range(3)] if i)
        rig.world.perverse = True  # now the fix makes things worse
        rig.clock.now += 61
        rig.engine.verify_visit("default", "Gate", "DAY")
        row = rig.store.decision(decision)
        assert row is not None and row["status"] == "reverted"
        assert rig.world.isp["aeTargetPercent"] == 50  # the prior value is back
        assert "aeTargetPercent" in rig.store.blocked_levers("default", "Gate", "DAY", row["problem"])
    finally:
        rig.close()


def test_blur_is_fixed_by_a_zoom_nudge_and_never_writes_isp(tmp_path: Path) -> None:
    rig = Rig(tmp_path, mode="act", world={"night": False, "glare": None})
    try:
        goto(rig, "Gate")
        rig.warm()
        rig.world.defocus = True
        decision = next(i for i in [rig.engine.check("default", "arrival") for _ in range(3)] if i)
        row = rig.store.decision(decision)
        assert row is not None and row["problem"] == "blur" and row["status"] == "applied"
        assert rig.world.defocus is False
        assert rig.world.isp_log == []
    finally:
        rig.close()


def test_frigate_and_people_always_win(rig: Rig) -> None:
    goto(rig, "BackPorch")
    rig.warm()
    before = len(rig.world.moves)
    held: list[dict[str, object]] = [{"auto_active_age_sec": 3.0}, {"manual_hold_remaining_sec": 12.0}]
    for signals in held:
        rig.world.signals = signals
        with pytest.raises(Refused):
            rig.engine.check("default", "manual")
        assert rig.engine.check("default", "arrival") is None
    assert len(rig.world.moves) == before and rig.world.isp_log == []
    rig.world.signals = {"auto_active_age_sec": 3600.0}  # an old track is not tracking
    assert any(rig.engine.check("default", "arrival") for _ in range(2))


def test_a_trial_uses_up_its_budget(tmp_path: Path) -> None:
    rig = Rig(tmp_path, world={"night": False, "glare": None}, guardrails={"trials_per_hour": 1})
    try:
        goto(rig, "Gate")
        rig.warm()
        rig.world.exposure = 1.5
        first = [rig.engine.check("default", "arrival") for _ in range(2)]
        assert any(first)
        assert rig.store.spent("default", "trial", 3600) == 1
        rig.clock.now += 400
        assert all(rig.engine.check("default", "arrival") is None for _ in range(3))
        assert rig.store.spent("default", "trial", 3600) == 1
    finally:
        rig.close()


def test_only_allowlisted_isp_keys_ever_reach_the_camera(tmp_path: Path) -> None:
    rig = Rig(tmp_path, mode="act", world={"night": False, "glare": None})
    try:
        goto(rig, "Gate")
        rig.warm()
        rig.world.exposure = 1.5
        for _ in range(4):
            rig.engine.check("default", "arrival")
            rig.clock.now += 400
        assert rig.world.isp_log
        assert all(set(write) <= ISP_ALLOWED for write in rig.world.isp_log)
    finally:
        rig.close()


def test_the_kill_switch_releases_and_restores(tmp_path: Path) -> None:
    rig = Rig(tmp_path, mode="act", world={"night": False, "glare": None})
    try:
        goto(rig, "Gate")
        rig.warm()
        rig.world.exposure = 1.5
        decision = next(i for i in [rig.engine.check("default", "arrival") for _ in range(3)] if i)
        rig.clock.now += 61
        rig.engine.verify_visit("default", "Gate", "DAY")
        rig.clock.now += 600
        rig.engine.verify_visit("default", "Gate", "DAY")
        assert rig.world.isp["aeTargetPercent"] == 45 and rig.store.decision(decision)["status"] == "kept"  # type: ignore[index]
        rig.engine.set_enabled(False)
        assert rig.world.isp["aeTargetPercent"] == 50
        with pytest.raises(Refused):
            rig.engine.check("default", "manual")
        assert not rig.engine.enabled
    finally:
        rig.close()


def test_observe_mode_only_logs(tmp_path: Path) -> None:
    rig = Rig(tmp_path, mode="observe")
    try:
        goto(rig, "BackPorch")
        rig.warm()
        decision = next(i for i in [rig.engine.check("default", "arrival") for _ in range(2)] if i)
        assert rig.store.decision(decision)["status"] == "observed"  # type: ignore[index]
        assert rig.world.isp_log == [] and "BackPorchNight" not in rig.world.preset_names()
    finally:
        rig.close()


def test_pending_suggestions_expire(rig: Rig) -> None:
    goto(rig, "BackPorch")
    rig.warm()
    decision = next(i for i in [rig.engine.check("default", "arrival") for _ in range(2)] if i)
    rig.clock.now += 73 * 3600
    rig.engine.expire_pending()
    assert rig.store.decision(decision)["status"] == "expired"  # type: ignore[index]


def test_the_loop_notices_the_patrol_arriving_and_checks_the_view(rig: Rig) -> None:
    """tick() sees the patrol's move counter change, waits for the camera to settle, then checks."""
    goto(rig, "BackPorch")
    rig.warm()
    patrol = rig.world.manager._runtimes[rig.cam.mac].patrol
    found = None
    for _ in range(3):
        patrol._index = 0  # the next move is BackPorch
        patrol._next_move_at = 0.0
        assert patrol.step()  # the patrol arrives
        rig.engine.tick("default")  # sees the new move, notes the arrival
        rig.clock.now += 2
        rig.engine.tick("default")  # settled: checks the view
        rig.clock.now += 400
        found = found or next(iter(rig.store.decisions(limit=1)), None)
    assert found is not None and found["trigger"] == "arrival" and found["problem"] == "glare"
    assert rig.store.profile("default", "BackPorch", "NIGHT") == {}  # nothing learned until something is kept


def test_main_opens_the_assist_door_only_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import main
    options = main.Options(host="192.0.2.10", cert=tmp_path / "osprey.pem")
    monkeypatch.delenv("OSPREY_ASSIST_TOKEN_FILE", raising=False)
    monkeypatch.delenv("OSPREY_ASSIST_SCOPES", raising=False)
    assert main.assist_link(options) is None
    enabled = main.Options(host="192.0.2.10", cert=tmp_path / "osprey.pem", assist={**options.assist, "enabled": True, "scopes": ["quality"]})
    link = main.assist_link(enabled)
    assert link is not None and link.scopes == frozenset({"quality"}) and len(link.token) >= 32
    assert (tmp_path / "assist-token").stat().st_mode & 0o777 == 0o600
    assert main.assist_link(enabled) == link  # the token survives a restart
    monkeypatch.setenv("OSPREY_ASSIST_TOKEN_FILE", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("OSPREY_ASSIST_SCOPES", "quality,presets")
    other = main.assist_link(options)
    assert other is not None and other.scopes == frozenset({"quality", "presets"}) and (tmp_path / "elsewhere").exists()
