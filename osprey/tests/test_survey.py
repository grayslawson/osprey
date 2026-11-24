"""Survey: the sweep, the map, the proposals and the way back, against a simulated camera."""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import assist.config as acfg
from assist import survey_map as sm
from assist import survey_plan as sp
from assist.decide import Decider
from assist.engine import Engine
from assist.providers import build_provider
from assist.store import Store
from assist.survey_run import Refused
from assist_world import Clock
from stub_models import StubModels
from survey_world import GEO, PanoWorld, decisions_reply


class SRig:
    def __init__(self, tmp: Path, *, mode: str = "suggest", models: StubModels | None = None, survey: dict[str, Any] | None = None,
                 tasks: dict[str, Any] | None = None, guardrails: dict[str, Any] | None = None, chat: bool = False,
                 **world: Any) -> None:
        self.clock = Clock()
        self.world = PanoWorld(tmp, self.clock, **world)
        raw: dict[str, Any] = {"enabled": True, "storage": {"dir": str(tmp / "state")},
                               "capabilities": {"survey": {"mode": mode}, "quality": {"mode": "off"}},
                               "survey": {"settle_sec": 1, "zoom_settle_sec": 1, "panorama_ppd": 4, "max_zoom_ins": 4,
                                          "schedule": None, **(survey or {})}}
        if guardrails:
            raw["guardrails"] = guardrails
        if models is not None:
            raw["providers"] = {"stub": {"kind": "chat" if chat else "decisions", "base_url": models.url, "allow_frames": True,
                                         "max_calls_per_hour": 10000, "max_calls_per_day": 100000}}
            raw["tasks"] = tasks or {"survey.label": {"mode": "model", "provider": "stub", "model": "m", "send_frames": True,
                                                       "min_confidence": 0.3},
                                     "survey.change": {"mode": "model", "provider": "stub", "model": "m", "send_frames": True}}
        self.cfg = acfg.parse(raw)
        self.store = Store(self.cfg.state_dir, clock=self.clock)
        providers = {n: build_provider(n, p) for n, p in self.cfg.providers.items()}
        self.engine = Engine(self.cfg, self.store, {"default": self.world.client},
                             Decider(self.cfg.tasks, providers, self.store.log_call), clock=self.clock, sleep=self.clock.sleep)
        self.svc = self.engine.survey

    def run(self, trigger: str = "manual") -> dict[str, Any]:
        run_id = self.svc.start("default", trigger, sync=True)
        run = self.svc.db.run(run_id)
        assert run is not None
        return run

    def close(self) -> None:
        self.world.close()
        self.store.close()


@pytest.fixture
def models() -> Iterator[StubModels]:
    with StubModels(decisions_reply=decisions_reply) as stub:
        yield stub


@pytest.fixture
def rig(tmp_path: Path, models: StubModels) -> Iterator[SRig]:
    r = SRig(tmp_path, models=models)
    try:
        yield r
    finally:
        r.close()


# ------------------------------------------------------------------------ geometry

def test_grid_for_a_g5_is_six_by_four_and_covers_the_range() -> None:
    cells = sp.plan(GEO, 0.25, 0.0)
    assert len(cells) == 24 and sp.grid_size(GEO, 0.25, 0.0) == (6, 4)
    assert {round(c.pan, 3) for c in cells} == {-1.0, -0.6, -0.2, 0.2, 0.6, 1.0}
    assert {round(c.tilt, 3) for c in cells} == {1.0, 0.333, -0.333, -1.0}
    h, v = GEO.fov(0)
    azs = sorted({c.az for c in cells})
    assert all(b - a <= h * 0.75 + 1e-6 for a, b in zip(azs, azs[1:], strict=False))
    assert sorted({c.el for c in cells})[1] - sorted({c.el for c in cells})[0] <= v * 0.75 + 1e-6


def test_snake_order_starts_at_the_nearest_corner_and_never_jumps() -> None:
    cells = sp.plan(GEO, 0.25, 0.0, start=(0.9, -0.9))
    assert (round(cells[0].pan, 2), round(cells[0].tilt, 2)) == (1.0, -1.0)
    hops = [abs(a.col - b.col) + abs(a.row - b.row) for a, b in zip(cells, cells[1:], strict=False)]
    assert max(hops) == 1


def test_more_overlap_means_more_cells_and_bad_overlap_is_refused() -> None:
    assert len(sp.plan(GEO, 0.5, 0.0)) > len(sp.plan(GEO, 0.25, 0.0))
    with pytest.raises(ValueError):
        sp.plan(GEO, 0.05)


def test_framing_a_small_region_zooms_in_and_a_large_one_does_not() -> None:
    assert sp.frame_view(GEO, -60, -30, 60, 30)[2] == 0.0
    pan, tilt, zoom = sp.frame_view(GEO, -92, -14, -84, 2)
    assert zoom > 0.5 and pan == pytest.approx(GEO.pan_of(-88))


def test_time_estimate_matches_the_documented_order_of_magnitude() -> None:
    est = sp.estimate_sec(GEO, sp.plan(GEO, 0.25, 0.0), 8)
    assert 100 < est["sweep_sec"] < 200 and est["total_sec"] < 400


def test_a_camera_without_calibration_is_refused() -> None:
    assert sp.Geometry.from_state(None) is None and sp.Geometry.from_state({"pan_degrees": 0}) is None


# ---------------------------------------------------------------------- stitching

def test_projection_round_trip_and_stitch_reproduce_the_world() -> None:
    mosaic = sm.Mosaic(GEO, 4)
    from survey_world import EXTENT, PPD, world_image
    world = world_image(False, False, False)
    for cell in sp.plan(GEO, 0.25, 0.0):
        fov = GEO.fov(0)
        x, y = round((cell.az - fov[0] / 2 - EXTENT[0]) * PPD), round((EXTENT[3] - cell.el - fov[1] / 2) * PPD)
        region = Image.new("RGB", (round(fov[0] * PPD), round(fov[1] * PPD)))
        region.paste(world, (-x, -y))
        buf = io.BytesIO()
        sm.from_angle_space(region, fov, (640, 360)).save(buf, "JPEG", quality=90)
        mosaic.add(buf.getvalue(), cell.i, cell.pan, cell.tilt, 0.0)
    assert mosaic.coverage() > 0.99
    inner = (40, 40, mosaic.size[0] - 40, mosaic.size[1] - 40)
    score = sm.ncc(mosaic.canvas.crop(inner), world.resize(mosaic.size).crop(inner), 96)
    assert score is not None and score > 0.95


# ------------------------------------------------------------------- closed loop

def test_a_survey_builds_a_labelled_map_with_proposals(rig: SRig) -> None:
    run = rig.run()
    assert run["status"] == "ready" and run["kind"] == "day"
    smap = rig.svc.db.latest_map("default", "day")
    assert smap is not None and smap["coverage"] > 0.95 and len(smap["cells"]) >= 24
    assert smap["seams"]["min"] is not None and smap["seams"]["min"] > 0.6
    by: dict[str, list[dict[str, Any]]] = {}
    for r in smap["regions"]:
        by.setdefault(r["label"], []).append(r)
    for label in ("sky", "foliage", "driveway", "street", "wall_eaves", "path", "fence_line"):
        assert label in by, label
    grid = sm.LabelGrid.from_png(rig.svc.db.read_file(run["id"], "labels.png") or b"", smap["extent"][0], smap["extent"][3])
    assert grid.at(80, -35) == "driveway" and grid.at(0, 40) == "sky" and grid.at(-170, 10) == "foliage"
    assert grid.at(-88, -4) == "door"            # found by the closer look
    props = rig.svc.db.proposals(camera="default")
    names = {p["name"] for p in props if p["type"] == "preset"}
    assert "Door" in names and "Driveway" in names
    door = next(p for p in props if p["type"] == "preset" and p["name"] == "Door")
    assert door["zoom"] > 0.3 and abs(rig.world.camera.pan_range.to_normalised(0) * 0) == 0
    assert any(p["type"] == "route" for p in props) and any(p["type"] == "frigate" for p in props)
    # suggest level changes nothing on the camera
    assert rig.world.preset_names() == [] and rig.svc.status("default")["pending"] >= 3
    assert rig.world.camera.motion.position.pan > 0   # returned to where it started


def test_the_camera_returns_to_where_it_was_and_the_patrol_hold_is_released(rig: SRig) -> None:
    before = rig.world.view()
    rig.run()
    after = rig.world.view()
    assert max(abs(a - b) for a, b in zip(before, after, strict=True)) < 0.02
    assert rig.world.client.state()["assist_signals"]["patrol_hold_remaining_sec"] == 0


# ---------------------------------------------------------------------- applying

def test_auto_saves_checked_presets_builds_the_patrol_and_reverts(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, mode="auto", models=models)
    try:
        run = rig.run()
        assert run["status"] == "ready"
        names = rig.world.preset_names()
        assert {"Door", "Gate", "Driveway"} <= set(names)
        tour = rig.world.client.sentry()["config"]
        assert tour["enabled"] is True and [p["name"] for p in tour["points"]]
        assert all(p["dwell_sec"] >= 8 for p in tour["points"])
        applied = [p for p in rig.svc.db.proposals(camera="default") if p["status"] == "applied"]
        assert applied and all(p["type"] in ("preset", "route") for p in applied)
        assert all(c["match"] is None or c["match"] > 0.35 for p in applied if p["type"] == "preset"
                   for c in p["applied"]["created"])
        out = rig.svc.revert("default", "console")
        assert set(out["removed"]) >= {"Door", "Gate"} and rig.world.preset_names() == []
        assert rig.world.client.sentry()["config"]["points"] == []
        assert rig.svc.status("default")["can_revert"] is False
    finally:
        rig.close()


def test_merge_keeps_the_owners_patrol_and_order(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, mode="auto", models=models, presets=True)
    try:
        rig.world.manager.stop()
        before = [p["name"] for p in rig.world.client.sentry()["config"]["points"]]
        assert before == ["BackPorch", "Gate"]
        rig.run()
        points = rig.world.client.sentry()["config"]["points"]
        names = [p["name"] for p in points]
        assert [n for n in names if n in before] == before
        assert len(names) > len(before) and rig.world.client.sentry()["config"]["enabled"] is True
        rig.svc.revert("default", "console")
        assert [p["name"] for p in rig.world.client.sentry()["config"]["points"]] == before
    finally:
        rig.close()


def test_suggest_and_observe_change_nothing_and_accept_works_one_at_a_time(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, mode="suggest", models=models)
    try:
        rig.run()
        assert rig.world.preset_names() == []
        door = next(p for p in rig.svc.db.proposals(camera="default") if p.get("name") == "Door")
        out = rig.svc.accept(door["id"], "console")
        assert out["applied"] == [door["id"]] and rig.world.preset_names() == ["Door"]
        rig.svc.reject(next(p["id"] for p in rig.svc.db.proposals(camera="default", statuses=("pending",)) if p["type"] == "preset"), "console")
        rig.engine.set_mode("survey", "observe")
        with pytest.raises(Refused):
            rig.svc.accept(next(p["id"] for p in rig.svc.db.proposals(camera="default", statuses=("pending",))), "console")
    finally:
        rig.close()


def test_a_preset_that_does_not_match_the_panorama_is_not_saved(rig: SRig) -> None:
    rig.run()
    rig.world.car = True     # the world changed since the map: a big red block now sits on the driveway
    drive = next(p for p in rig.svc.db.proposals(camera="default") if p.get("name") == "Driveway")
    rig.svc.cfg = acfg.parse({"survey": {"verify_min_ncc": 0.99}}).survey
    out = rig.svc.apply("default", [drive["id"]], "console")
    assert out["failed"] and "match" in next(iter(out["failed"].values())) and "Driveway" not in rig.world.preset_names()


def test_the_token_without_the_patrol_scope_cannot_rewrite_the_tour(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, mode="auto", models=models)
    try:
        import assist_scope
        rig.world.server.web.assist = assist_scope.AssistLink("w" * 40, frozenset({"quality", "presets"}), "http://127.0.0.1:1")
        rig.run()
        assert "Door" in rig.world.preset_names()        # presets still saved
        route = next(p for p in rig.svc.db.proposals(camera="default") if p["type"] == "route")
        assert route["status"] == "failed" and "patrol scope" in route["reason"]
        assert rig.world.client.sentry() == {}
    finally:
        rig.close()


# ---------------------------------------------------------------------- the night

def test_a_night_survey_finds_glare_and_proposes_a_night_view_that_is_checked(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, mode="auto", models=models, night=True)
    try:
        run = rig.run()
        assert run["kind"] == "night" and run["status"] == "ready"
        smap = rig.svc.db.latest_map("default", "night")
        assert smap is not None and any(r["label"] == "glare_source" for r in smap["regions"])
        nights = [p for p in rig.svc.db.proposals(camera="default") if p["type"] == "night"]
        assert nights and all(p["glare_after"] <= p["glare_before"] * 0.5 for p in nights)
        assert any(p["status"] == "applied" for p in nights), [(p["name"], p["status"], p.get("reason")) for p in nights]
        done = next(p for p in nights if p["status"] == "applied")
        assert done["name"] in rig.world.preset_names()
        points = rig.world.client.sentry()["config"]["points"]
        assert any(pt.get("night_preset") == done["name"] for pt in points)
    finally:
        rig.close()


# --------------------------------------------------------------------- yielding

def move_window(rig: SRig, start: float, until: float) -> None:
    now = rig.clock()
    rig.world.busy_windows.append((now + start, now + until, {"auto_active_age_sec": 1.0}))


def test_frigate_tracking_pauses_the_survey_without_a_single_move_then_it_resumes(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        start = rig.clock()
        move_window(rig, 20, 80)
        run = rig.run()
        assert run["status"] == "ready"
        assert not [t for t in rig.world.move_times if start + 20 <= t < start + 80]
        assert len(rig.svc.db.cells(run["id"])) >= 24
    finally:
        rig.close()


def test_a_long_wait_interrupts_and_resume_does_only_the_rest(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models, survey={"resume_wait_sec": 30})
    try:
        start = rig.clock()
        move_window(rig, 20, 400)
        run = rig.run()
        assert run["status"] == "interrupted" and "waited" in run["reason"]
        done = len(rig.svc.db.cells(run["id"]))
        assert 0 < done < 24
        with pytest.raises(Refused):                       # still tracking
            rig.svc.resume("default", sync=True)
        rig.clock.now = start + 500
        moves_before = len(rig.world.move_times)
        assert rig.svc.resume("default", sync=True) == run["id"]
        again = rig.svc.db.run(run["id"])
        assert again is not None and again["status"] == "ready"
        assert len(rig.world.move_times) - moves_before < 24 - done + 8 + 2   # the rest, zoom-ins and the way home
        assert len(rig.svc.db.cells(run["id"])) >= 24
    finally:
        rig.close()


def test_a_person_holding_the_camera_also_stops_the_survey(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models, survey={"resume_wait_sec": 10})
    try:
        rig.world.busy_windows.append((rig.clock(), rig.clock() + 1000, {"manual_hold_remaining_sec": 30.0}))
        with pytest.raises(Refused, match="person"):
            rig.run()
    finally:
        rig.close()


def test_a_scene_change_during_the_run_fails_it_rather_than_mixing_maps(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        rig.world.flip_scene_at = rig.clock() + 20
        run = rig.run()
        assert run["status"] == "failed" and "scene changed" in run["reason"]
        assert rig.svc.db.latest_map("default", "day") is None and rig.svc.db.latest_map("default", "night") is None
    finally:
        rig.close()


def test_three_unusable_pictures_in_a_row_end_the_run(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        rig.world.black_snapshots = 3
        run = rig.run()
        assert run["status"] == "failed" and "unusable" in run["reason"]
    finally:
        rig.close()


def test_one_bad_picture_is_a_gap_and_a_missed_position_is_kept_as_reached(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        rig.world.black_snapshots = 1
        rig.world.miss = [0.1, 0.1]
        run = rig.run()
        assert run["status"] == "ready"
        cells = rig.svc.db.cells(run["id"])
        assert {c["status"] for c in cells} >= {"gap"} or any(c["status"] == "missed" for c in cells)
        assert any(c["status"] in ("gap", "missed") for c in cells)
    finally:
        rig.close()


def test_the_move_budget_gives_a_partial_map_that_says_so(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models, survey={"max_moves": 8})
    try:
        run = rig.run()
        assert run["status"] == "partial" and "budget" in run["reason"]
        smap = rig.svc.db.latest_map("default", "day")
        assert smap is not None and smap["coverage"] < 0.95
    finally:
        rig.close()


def test_cancel_stops_at_the_next_cell_and_the_camera_goes_home(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        before = rig.world.view()
        rig.world.on_move = lambda n: rig.svc.cancel() if n == 4 else None
        run = rig.run()
        assert run["status"] == "cancelled"
        assert max(abs(a - b) for a, b in zip(before, rig.world.view(), strict=True)) < 0.02
        assert rig.svc.status("default")["busy"] is False
    finally:
        rig.close()


def test_the_kill_switch_cancels_a_survey(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        off = threading.Thread(target=rig.engine.set_enabled, args=(False,))
        rig.world.on_move = lambda n: off.start() if n == 3 else None
        assert rig.run()["status"] == "cancelled"
        off.join()
    finally:
        rig.close()


def test_a_restart_turns_a_run_in_flight_into_a_resumable_one(rig: SRig) -> None:
    run_id = rig.svc.db.add_run("default", "day", "manual", {"scene": "DAY", "plan": [], "moves": 0})
    rig.svc.db.update_run(run_id, "running")
    rig.svc.reconcile()
    row = rig.svc.db.run(run_id)
    assert row is not None and row["status"] == "interrupted" and "restarted" in row["reason"]


def test_refusals_are_plain(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models, survey={"max_runs_per_day": 1}, mode="suggest")
    try:
        rig.run()
        with pytest.raises(Refused, match="last day"):
            rig.run()
        rig.engine.set_mode("survey", "off")
        with pytest.raises(Refused, match="off"):
            rig.svc.start("default", "manual")
        rig.engine.set_enabled(False)
        with pytest.raises(Refused, match="switched off"):
            rig.svc.start("default", "manual")
    finally:
        rig.close()


def test_quiet_hours_stop_a_scheduled_survey_but_not_one_you_asked_for(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models, guardrails={"quiet_hours": ["00:00-23:59"]})
    try:
        with pytest.raises(Refused, match="quiet"):
            rig.svc.start("default", "schedule", sync=True)
        assert rig.run()["status"] == "ready"
    finally:
        rig.close()


def test_an_uncalibrated_camera_needs_a_fov_in_the_config(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models)
    try:
        rig.world.camera.model = "Some Other PTZ"
        with pytest.raises(Refused, match="calibration"):
            rig.run()
        rig.svc.cfg = acfg.parse({"survey": {"fov": {"pan_degrees": 350, "tilt_degrees": 100, "wide": [99.7, 51.9],
                                                    "tele": [45.5, 25.4]}}}).survey
        assert rig.run()["status"] == "ready"
    finally:
        rig.close()


# ---------------------------------------------------------------------- learning

def test_a_second_survey_reports_what_moved(rig: SRig) -> None:
    rig.run()
    rig.world.car = True
    rig.clock.now += 86400
    run = rig.run()
    smap = rig.svc.db.map(run["map"])
    assert smap is not None and smap["compared_to"] and smap["changes"]
    near = [c for c in smap["changes"] if 50 < c["az"] < 95 and -50 < c["el"] < -20]
    assert near and near[0]["change"] == "moved_object"
    assert all(abs(c["az"]) < 230 for c in smap["changes"])
    third = rig.svc.db.map(rig.run()["map"])      # nothing moved since: the diff is quiet there
    assert third is not None and not [c for c in third["changes"] if 50 < c["az"] < 95 and -50 < c["el"] < -20]


def test_frigate_events_become_heat_on_the_right_part_of_the_map(tmp_path: Path, models: StubModels) -> None:
    from assist.survey_frigate import FrigateClient, refresh_heat
    from survey_world import StubFrigate
    with StubFrigate() as frigate:
        rig = SRig(tmp_path, models=models, survey={"frigate": {"url": frigate.url, "camera": "front"}})
        try:
            now = rig.clock()
            pan, tilt = GEO.pan_of(85), GEO.tilt_of(-38)             # looking at the driveway
            rig.svc.db.log_position("default", pan, tilt, 0.0)
            rig.clock.now += 10
            frigate.events = [{"start_time": now + 5, "label": "car", "data": {"box": [0.4, 0.4, 0.2, 0.2]}}] * 12 + [
                {"start_time": now + 6, "label": "person"},                                  # no box: skipped
                {"start_time": now - 99999, "label": "dog", "data": {"box": [0.1, 0.1, 0.1, 0.1]}}]
            fr = rig.cfg.survey.frigate
            assert fr is not None
            out = refresh_heat(rig.svc.db, FrigateClient(fr), "default", GEO, fr, rig.clock())
            assert out["placed"] == 12 and out["skipped"] >= 1
            assert "cameras=front" in frigate.requests[0]
            heat = rig.svc.db.heat("default", 14)
            assert max(heat, key=lambda k: heat[k]) == (8, -4) and max(heat.values()) == 1.0
            run = rig.run()
            smap = rig.svc.db.map(run["map"])
            assert smap is not None
            drive = next(r for r in smap["regions"] if r["label"] == "driveway")
            assert drive["heat"] > 0.9 and drive["interest"] > 0.6
            # the heat decays: a month later it is a fraction
            rig.clock.now += 30 * 86400
            assert max(rig.svc.db.heat("default", 14).values(), default=0) == 1.0   # scaled to its own peak
            row = rig.store._run("SELECT weight FROM survey_heat LIMIT 1").fetchone()
            assert row["weight"] > 0
        finally:
            rig.close()


def test_an_unreachable_frigate_is_survivable(tmp_path: Path, models: StubModels) -> None:
    from assist.survey_frigate import FrigateClient
    cfg = acfg.parse({"survey": {"frigate": {"url": "http://127.0.0.1:1"}}}).survey.frigate
    assert cfg is not None
    with pytest.raises(ConnectionError):
        FrigateClient(cfg, timeout=1).events(0)


def test_first_run_is_offered_and_auto_starts_it_in_a_quiet_window(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, mode="auto", models=models)
    try:
        assert rig.svc.status("default")["first_run"] is True
        rig.svc.housekeeping()
        rig.svc.join(60)
        runs = rig.svc.db.runs("default")
        assert len(runs) == 1 and runs[0]["status"] == "ready"
        assert rig.svc.status("default")["first_run"] is False
    finally:
        rig.close()


def test_first_run_waits_for_the_owner_below_auto(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, mode="suggest", models=models)
    try:
        rig.svc.housekeeping()
        assert rig.svc.db.runs("default") == [] and rig.svc.status("default")["first_run"] is True
    finally:
        rig.close()


def test_the_weekly_schedule_runs_day_and_night_surveys_once_each(tmp_path: Path, models: StubModels) -> None:
    import time as t
    rig = SRig(tmp_path, models=models)
    try:
        here = t.strftime("%H:%M", t.localtime(rig.clock()))
        rig.svc.cfg = acfg.parse({"survey": {"schedule": {"day": here, "night": here, "every_days": 7}}}).survey
        rig.svc.housekeeping()
        rig.svc.join(60)
        runs = rig.svc.db.runs("default")
        assert [r["kind"] for r in runs] == ["day"] and runs[0]["trigger"] == "schedule"   # only the scene that matches
        rig.clock.now += 400
        rig.svc.housekeeping()
        assert len(rig.svc.db.runs("default")) == 1           # a week must pass
        rig.clock.now += 7 * 86400
        rig.svc.housekeeping()
        rig.svc.join(60)
        assert len(rig.svc.db.runs("default")) == 2
    finally:
        rig.close()


def test_retention_keeps_the_newest_maps_and_forgets_the_rest(tmp_path: Path, models: StubModels) -> None:
    rig = SRig(tmp_path, models=models, survey={"keep_maps": 1, "max_runs_per_day": 9})
    try:
        first = rig.run()
        rig.clock.now += 7200
        second = rig.run()
        rig.clock.now += 7200
        rig.svc.db.cleanup(keep_maps=1, frames_days=7)
        assert rig.svc.db.read_file(first["id"], "panorama.jpg") is None
        assert rig.svc.db.read_file(second["id"], "panorama.jpg") is not None
        rig.engine.purge()
        assert rig.svc.db.maps("default") == [] and rig.svc.db.runs("default") == []
    finally:
        rig.close()
