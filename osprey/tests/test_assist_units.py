"""Assist's config, store, baselines, detector and levers, one at a time."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

import assist.config as acfg
import assist.levers as lv
from assist.detect import Baselines, Detector
from assist.store import Store
from assist.types import Metrics


def metrics(**over: float) -> Metrics:
    base: dict[str, Any] = {"sharp": 150.0, "clip_hi": 0.0, "clip_lo": 0.0, "luma": 100.0, "contrast": 120.0, "noise": 1.0,
                            "glare": 0.0, "glare_cx": None, "glare_cy": None, "glare_map": ()}
    base.update(over)
    return Metrics(**base)


# ---------------------------------------------------------------- config


def test_defaults_are_off_and_safe() -> None:
    cfg = acfg.parse({})
    assert cfg.enabled is False and cfg.modes == {"quality": "suggest", "patrol": "off", "followup": "off", "presets": "off", "survey": "suggest"}
    assert cfg.guardrails.structural_in_auto is True and cfg.storage.frames_days == 14 and cfg.storage.followup_frames_days == 3
    assert set(cfg.tasks) == set(acfg.TASKS) and all(t.mode == "rules" for t in cfg.tasks.values())
    assert cfg.listen.bind == "127.0.0.1"


@pytest.mark.parametrize("raw", [
    {"nope": 1}, {"osprey": {"url": "ftp://x"}}, {"listen": {"port": 0}}, {"capabilities": {"magic": {}}},
    {"capabilities": {"quality": {"mode": "reckless"}}}, {"capabilities": {"quality": {"levers": ["wdr"]}}},
    {"capabilities": {"quality": {"levers": ["gain"]}}}, {"tasks": {"quality.nope": {}}},
    {"tasks": {"quality.diagnose": {"mode": "model", "provider": "x", "model": "m"}}},
    {"guardrails": {"quiet_hours": ["25:00-26:00"]}}, {"storage": {"frames_days": 0}}, {"providers": {"p": {"kind": "chat"}}},
    {"capabilities": {"quality": {"problems": ["rust"]}}}, {"privacy": {"debug_prompts": "yes"}},
])
def test_bad_config_is_refused(raw: Any) -> None:
    with pytest.raises(ValueError):
        acfg.parse(raw)


def test_a_lever_with_unknown_units_needs_explicit_limits() -> None:
    cfg = acfg.parse({"capabilities": {"quality": {"levers": ["aeTargetPercent", "gain"],
                                                    "limits": {"gain": {"lo": 0, "hi": 30, "step": 3, "default": 12}}}}})
    levers = lv.build_levers(list(cfg.quality.levers), cfg.quality.limits)
    assert levers["gain"].hi == 30 and levers["aeTargetPercent"].step == 5


def test_a_config_file_loads(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text('{"enabled": true, "listen": {"port": 9000}}', encoding="utf-8")
    assert acfg.load(path).listen.port == 9000
    assert acfg.load(tmp_path / "missing.json").enabled is False
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        acfg.load(path)


# ----------------------------------------------------------------- levers


def test_ladders_follow_the_scene_and_skip_what_is_off_or_cooling() -> None:
    levers = lv.build_levers(["aeTargetPercent", "hdr", "night_view", "zoom_nudge", "sharpness"], {})
    night = [a.lever for a in lv.candidates("glare", "NIGHT", levers)]
    assert night == ["night_view", "aeTargetPercent", "hdr"]
    assert [a.lever for a in lv.candidates("glare", "DAY", levers)] == ["aeTargetPercent", "hdr"]
    assert [a.lever for a in lv.candidates("glare", "NIGHT", levers, frozenset({"night_view"}))] == ["aeTargetPercent", "hdr"]
    assert [a.lever for a in lv.candidates("blur", "DAY", levers)] == ["zoom_nudge", "sharpness"]  # touch_focus is opt-in
    assert lv.candidates("noise", "DAY", levers) == ()


def test_values_are_stepped_clamped_and_stop_at_the_limit() -> None:
    ae = lv.DEFAULT_LEVERS["aeTargetPercent"]
    assert lv.next_value(ae, {}, -5) == 45 and lv.next_value(ae, {"aeTargetPercent": 22}, -5) == 20
    assert lv.next_value(ae, {"aeTargetPercent": 20}, -5) is None
    assert lv.next_value(ae, {"aeTargetPercent": 47}, 5) == 50


def test_the_denied_keys_can_never_be_levers() -> None:
    for key in ("focusPosition", "irLedLevel", "irLedMode", "wdr"):
        assert key not in lv.DEFAULT_LEVERS
        with pytest.raises(ValueError):
            lv.build_levers([key], {})


# ----------------------------------------------------- baselines and detector


def test_a_baseline_learns_and_z_scores(tmp_path: Path) -> None:
    book = Baselines(Store(tmp_path))
    for i in range(30):
        book.learn("c", "p", "DAY", 12, metrics(sharp=150 + (i % 3), luma=100 + (i % 2)))
    z = book.z("c", "p", "DAY", 12, metrics(sharp=100, luma=100))
    assert z["sharp"] < -3
    assert abs(book.z("c", "p", "DAY", 12, metrics())["sharp"]) < 2
    assert book.z("c", "p", "DAY", 3, metrics(sharp=100))["sharp"] < -3  # a quiet hour falls back to the scene


def test_priors_let_glare_fire_before_any_baseline(tmp_path: Path) -> None:
    z = Baselines(Store(tmp_path)).z("c", "p", "NIGHT", 2, metrics(glare=0.2))
    assert z["glare"] > 2 and z["sharp"] == 0.0


def test_two_samples_to_fire_and_hysteresis_to_clear(tmp_path: Path) -> None:
    det = Detector()
    probs = ("glare",)
    z_bad = {"sharp": 0, "luma": 0, "noise": 0, "glare": 5.0, "clip_hi": 0, "clip_lo": 0, "contrast": 0}
    z_grey = {**z_bad, "glare": 1.5}
    z_ok = {**z_bad, "glare": 0.2}
    bad = metrics(glare=0.2)
    assert det.evaluate(bad, z_bad, problems=probs) == []  # one sample is not enough
    assert [f.problem for f in det.evaluate(bad, z_bad, problems=probs)] == ["glare"]
    assert [f.problem for f in det.evaluate(metrics(glare=0.05), z_grey, problems=probs)] == ["glare"]  # grey band: still on
    assert det.evaluate(metrics(), z_ok, problems=probs) == []
    assert det.evaluate(bad, z_bad, problems=probs) == []  # it has to prove itself twice again


def test_blur_and_noise_cannot_fire_without_a_baseline_and_blur_ignores_blown_frames() -> None:
    det = Detector()
    z = {"sharp": -9.0, "luma": 0, "noise": 0, "glare": 0, "clip_hi": 0, "clip_lo": 0, "contrast": 0}
    det.evaluate(metrics(sharp=1), z, problems=("blur",))
    assert [f.problem for f in det.evaluate(metrics(sharp=1), z, problems=("blur",))] == ["blur"]
    blown = Detector()
    blown.evaluate(metrics(sharp=1, clip_hi=0.5), z, problems=("blur",))
    assert blown.evaluate(metrics(sharp=1, clip_hi=0.5), z, problems=("blur",)) == []


# ------------------------------------------------------------------ store


def test_the_state_directory_and_files_are_private(tmp_path: Path) -> None:
    store = Store(tmp_path / "state")
    name = store.save_frame(b"\xff\xd8x")
    path = store.frame_path(name)
    assert path is not None
    assert stat.S_IMODE(os.stat(tmp_path / "state").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "state" / "assist.db").st_mode) == 0o600


def test_frame_names_cannot_escape_the_frames_folder(tmp_path: Path) -> None:
    store = Store(tmp_path)
    for bad in ("../../etc/passwd", "20261124_zz", "..", "20261124_abcd1234/../../x", ""):
        assert store.frame_path(bad) is None and store.frame(bad) is None


def test_retention_follows_the_owner_choices(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    store = Store(tmp_path, clock=lambda: now[0])
    old_quality = store.save_frame(b"q", "q")
    old_follow = store.save_frame(b"f", "f")
    pending = store.save_frame(b"p", "q")
    store.add_decision({"camera": "c", "preset": "p", "scene": "DAY", "trigger": "x", "problem": "glare", "confidence": 1,
                        "source": "rules", "status": "pending", "before_frame": pending}, {"plan": {}})
    store.add_sample("c", "p", "DAY", "x", {})
    for name in (old_quality, old_follow, pending):
        os.utime(store.frame_path(name), (now[0] - 20 * 86400,) * 2)  # type: ignore[arg-type]
    fresh_follow = store.save_frame(b"f2", "f")
    os.utime(store.frame_path(fresh_follow), (now[0] - 2 * 86400,) * 2)  # type: ignore[arg-type]
    later = store.save_frame(b"f3", "f")
    os.utime(store.frame_path(later), (now[0] - 4 * 86400,) * 2)  # type: ignore[arg-type]
    out = store.cleanup(frames_days=14, followup_days=3, samples_days=90, decisions_days=365, max_frames_mb=100)
    assert store.frame(old_quality) is None      # older than 14 days
    assert store.frame(old_follow) is None       # people frames go after 3
    assert store.frame(later) is None            # 4 days old: past the follow-up limit
    assert store.frame(fresh_follow) == b"f2"
    assert store.frame(pending) == b"p"          # a waiting suggestion keeps its frames
    assert out["frames"] == 3
    now[0] += 100 * 86400
    store.cleanup(frames_days=14, followup_days=3, samples_days=90, decisions_days=365, max_frames_mb=100)
    assert store._run("SELECT COUNT(*) AS c FROM samples").fetchone()["c"] == 0


def test_the_size_cap_drops_the_oldest_unpinned_frames_first(tmp_path: Path) -> None:
    store = Store(tmp_path)
    names = [store.save_frame(b"x" * 600_000) for _ in range(4)]
    for i, name in enumerate(names):
        os.utime(store.frame_path(name), (time.time() - 100 + i,) * 2)  # type: ignore[arg-type]
    store.cleanup(frames_days=14, followup_days=3, samples_days=90, decisions_days=365, max_frames_mb=1)
    assert store.frame(names[0]) is None and store.frame(names[3]) is not None


def test_purge_forgets_everything_but_keeps_the_settings(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.put("enabled", True)
    store.save_frame(b"x")
    store.add_sample("c", "p", "DAY", "x", {})
    store.purge()
    assert list(store.frames_dir.glob("*/*.jpg")) == [] and store.get("enabled") is True
    assert store._run("SELECT COUNT(*) AS c FROM samples").fetchone()["c"] == 0
