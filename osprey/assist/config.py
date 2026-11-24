"""The Assist config file (``osprey-assist.json``): strict, like the core's config."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .decide import TaskConfig, parse_task
from .levers import DEFAULT_LEVERS, build_levers
from .notify import NotifyConfig, parse_notify
from .providers import ProviderConfig, parse_provider
from .types import MODES, PROBLEMS

DEFAULT_PATH: Final = "/var/lib/osprey-assist/osprey-assist.json"
TASKS: Final = ("quality.diagnose", "quality.judge", "quality.glare", "quality.blur", "quality.action", "survey.label", "survey.interest", "survey.change")
SURVEY_TASKS: Final = ("survey.label", "survey.interest", "survey.change")
CAPABILITIES: Final = ("quality", "patrol", "followup", "presets", "survey")
IMPLEMENTED: Final = ("quality", "survey")


def _only(raw: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f'"{where}" must be an object')
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f'unknown key(s) in "{where}": {", ".join(sorted(unknown))}')
    return raw


def _num(raw: dict[str, Any], key: str, default: float, lo: float, hi: float, where: str) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise ValueError(f'"{where}.{key}" must be a number from {lo:g} to {hi:g}')
    return float(value)


def _flag(raw: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f'"{where}.{key}" must be true or false')
    return value


def _text(raw: dict[str, Any], key: str, default: str | None, where: str) -> str | None:
    value = raw.get(key, default)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f'"{where}.{key}" must be text')
    return value


@dataclass(frozen=True)
class OspreyLink:
    url: str = "http://127.0.0.1:8000"
    token_file: str = "/var/lib/osprey/assist-token"
    cameras: tuple[str, ...] = ("default",)


@dataclass(frozen=True)
class Listen:
    bind: str = "127.0.0.1"
    port: int = 8090


@dataclass(frozen=True)
class Storage:
    dir: str = "/var/lib/osprey-assist"
    frame_store_px: int = 1280
    frames_days: float = 14
    followup_frames_days: float = 3
    samples_days: float = 90
    decisions_days: float = 365
    max_frames_mb: int = 2048


@dataclass(frozen=True)
class Quality:
    mode: str = "suggest"
    problems: tuple[str, ...] = ("glare", "blur", "under", "over", "noise")
    levers: tuple[str, ...] = ("aeTargetPercent", "brightness", "contrast", "sharpness", "denoise", "hdr",
                               "zoom_nudge", "night_view")
    limits: dict[str, dict[str, float]] = field(default_factory=dict)
    periodic_min: float = 10
    settle_sec: float = 3
    isp_settle_sec: float = 4
    verify_after_sec: tuple[float, ...] = (60, 600)
    min_gain: float = 0.10
    max_regress: float = 0.10
    fire_thresholds: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Guardrails:
    moves_per_hour: int = 6
    isp_writes_per_hour: int = 6
    trials_per_hour: int = 4
    probe_sessions_per_night: int = 2
    max_probe_offsets: int = 8
    max_offset_frac: float = 0.04
    preset_changes_per_day: int = 2
    structural_in_auto: bool = True
    tracking_hold_sec: float = 15
    pending_expire_hours: float = 72
    quiet_hours: tuple[str, ...] = ()
    quiet_isp: bool = False


@dataclass(frozen=True)
class Frigate:
    url: str
    camera: str | None = None
    api_key_file: str | None = None
    lookback_days: float = 14
    refresh_min: float = 60
    half_life_days: float = 14
    # Who writes PTZ zones into Frigate: nobody ("off"), Osprey after you accept them in the console ("osprey"), or Kestrel,
    # which fetches them from Survey's API ("kestrel"). One writer per Frigate.
    writer: str = "off"


@dataclass(frozen=True)
class Schedule:
    day: str | None = "11:00"
    night: str | None = "02:00"
    every_days: float = 7


@dataclass(frozen=True)
class Survey:
    overlap: float = 0.25
    wide_zoom: float = 0.0
    tiles: tuple[int, int] = (4, 3)
    max_zoom_ins: int = 8
    zoom_in_max: float = 0.7
    settle_sec: float = 2.0
    zoom_settle_sec: float = 4.0
    speed: float = 1.0
    max_moves: int = 120
    max_minutes: float = 25
    resume_wait_sec: float = 600
    max_runs_per_day: int = 3
    model_concurrency: int = 4
    max_presets: int = 10
    min_interest: float = 0.35
    dwell: dict[str, float] = field(default_factory=lambda: {"base_sec": 20.0, "min_sec": 8.0, "max_sec": 60.0})
    patrol_policy: str = "merge"
    enable_patrol: bool = True
    keep_maps: int = 4
    frames_days: float = 7
    verify_min_ncc: float = 0.35
    projection: str = "rectilinear"
    panorama_ppd: float = 8
    fov: dict[str, Any] | None = None
    schedule: Schedule | None = field(default_factory=Schedule)
    frigate: Frigate | None = None


@dataclass(frozen=True)
class Privacy:
    redact_names: bool = False
    debug_prompts: bool = False


@dataclass(frozen=True)
class AssistConfig:
    enabled: bool = False
    osprey: OspreyLink = field(default_factory=OspreyLink)
    listen: Listen = field(default_factory=Listen)
    storage: Storage = field(default_factory=Storage)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    tasks: dict[str, TaskConfig] = field(default_factory=lambda: {name: TaskConfig() for name in TASKS})
    quality: Quality = field(default_factory=Quality)
    modes: dict[str, str] = field(default_factory=lambda: {"quality": "suggest", "patrol": "off",
                                                          "followup": "off", "presets": "off", "survey": "suggest"})
    guardrails: Guardrails = field(default_factory=Guardrails)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    privacy: Privacy = field(default_factory=Privacy)
    survey: Survey = field(default_factory=Survey)

    @property
    def state_dir(self) -> Path:
        return Path(self.storage.dir)


def _mode(value: Any, where: str) -> str:
    if value not in MODES:
        raise ValueError(f'"{where}" must be one of {", ".join(MODES)}')
    return str(value)


def _hhmm(items: Any, where: str) -> tuple[str, ...]:
    out: list[str] = []
    if not isinstance(items, list):
        raise ValueError(f'"{where}" must be a list of "HH:MM-HH:MM" ranges')
    for item in items:
        ok = isinstance(item, str) and len(item) == 11 and item[2] == ":" and item[5] == "-" and item[8] == ":"
        if ok:
            try:
                parts = [int(item[0:2]), int(item[3:5]), int(item[6:8]), int(item[9:11])]
                ok = parts[0] < 24 and parts[2] < 24 and parts[1] < 60 and parts[3] < 60
            except ValueError:
                ok = False
        if not ok:
            raise ValueError(f'"{where}" entries look like "22:00-06:00"')
        out.append(str(item))
    return tuple(out)


def _hhmm1(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not (isinstance(value, str) and len(value) == 5 and value[2] == ":" and value[:2].isdigit()
            and value[3:].isdigit() and int(value[:2]) < 24 and int(value[3:]) < 60):
        raise ValueError(f'"{where}" must look like "11:00"')
    return value


def parse_survey(raw: Any) -> Survey:
    keys = {"overlap", "wide_zoom", "tiles", "max_zoom_ins", "zoom_in_max", "settle_sec", "zoom_settle_sec", "speed",
            "max_moves", "max_minutes", "resume_wait_sec", "max_runs_per_day", "model_concurrency", "max_presets",
            "min_interest", "dwell", "patrol_policy", "enable_patrol", "keep_maps", "frames_days", "verify_min_ncc",
            "projection", "panorama_ppd", "fov", "schedule", "frigate"}
    r = _only(raw, keys, "survey")
    w = "survey"
    tiles = r.get("tiles", [4, 3])
    if (not isinstance(tiles, list) or len(tiles) != 2 or not all(isinstance(t, int) and not isinstance(t, bool)
                                                                   and 1 <= t <= 8 for t in tiles)):
        raise ValueError('"survey.tiles" must be [across, down], each 1 to 8')
    dwell = dict(Survey().dwell)
    given = r.get("dwell", {})
    if not isinstance(given, dict) or set(given) - set(dwell):
        raise ValueError('"survey.dwell" may hold base_sec, min_sec and max_sec')
    for k in given:
        dwell[k] = _num(given, k, dwell[k], 1, 600, "survey.dwell")
    if not dwell["min_sec"] <= dwell["base_sec"] <= dwell["max_sec"]:
        raise ValueError('"survey.dwell" needs min_sec <= base_sec <= max_sec')
    policy = r.get("patrol_policy", "merge")
    if policy not in ("merge", "replace"):
        raise ValueError('"survey.patrol_policy" must be merge or replace')
    projection = r.get("projection", "rectilinear")
    if projection not in ("rectilinear", "linear"):
        raise ValueError('"survey.projection" must be rectilinear or linear')
    fov = r.get("fov")
    if fov is not None:
        from .survey_plan import Geometry
        try:
            Geometry.from_config(fov)
        except (KeyError, TypeError, ValueError, IndexError):
            raise ValueError('"survey.fov" needs pan_degrees, tilt_degrees, wide [h, v] and tele [h, v], all positive') from None
    sched: Schedule | None = Schedule()
    if "schedule" in r:
        sraw = r["schedule"]
        if sraw is None:
            sched = None
        else:
            s = _only(sraw, {"day", "night", "every_days"}, "survey.schedule")
            sched = Schedule(_hhmm1(s.get("day"), "survey.schedule.day") if "day" in s else None,
                             _hhmm1(s.get("night"), "survey.schedule.night") if "night" in s else None,
                             _num(s, "every_days", 7, 1, 365, "survey.schedule"))
    frigate: Frigate | None = None
    if r.get("frigate") is not None:
        f = _only(r["frigate"], {"url", "camera", "api_key_file", "lookback_days", "refresh_min", "half_life_days", "writer"},
                  "survey.frigate")
        url = _text(f, "url", None, "survey.frigate")
        if not url or not url.startswith(("http://", "https://")):
            raise ValueError('"survey.frigate.url" must be an http or https URL')
        writer = f.get("writer", "off")
        if writer not in ("off", "osprey", "kestrel"):
            raise ValueError('"survey.frigate.writer" must be off, osprey or kestrel')
        if writer == "osprey" and not f.get("camera"):
            raise ValueError('"survey.frigate.writer": "osprey" needs "survey.frigate.camera", the camera\'s name in Frigate')
        frigate = Frigate(url.rstrip("/"), _text(f, "camera", None, "survey.frigate"),
                          _text(f, "api_key_file", None, "survey.frigate"),
                          _num(f, "lookback_days", 14, 1, 365, "survey.frigate"),
                          _num(f, "refresh_min", 60, 5, 1440, "survey.frigate"),
                          _num(f, "half_life_days", 14, 1, 365, "survey.frigate"), str(writer))
    return Survey(
        _num(r, "overlap", 0.25, 0.15, 0.6, w), _num(r, "wide_zoom", 0.0, 0, 1, w), (tiles[0], tiles[1]),
        int(_num(r, "max_zoom_ins", 8, 0, 40, w)), _num(r, "zoom_in_max", 0.7, 0, 1, w),
        _num(r, "settle_sec", 2.0, 0, 60, w), _num(r, "zoom_settle_sec", 4.0, 0, 60, w), _num(r, "speed", 1.0, 0.05, 1, w),
        int(_num(r, "max_moves", 120, 1, 2000, w)), _num(r, "max_minutes", 25, 1, 240, w),
        _num(r, "resume_wait_sec", 600, 0, 86400, w), int(_num(r, "max_runs_per_day", 3, 1, 48, w)),
        int(_num(r, "model_concurrency", 4, 1, 32, w)), int(_num(r, "max_presets", 10, 1, 40, w)),
        _num(r, "min_interest", 0.35, 0, 1, w), dwell, str(policy), _flag(r, "enable_patrol", True, w),
        int(_num(r, "keep_maps", 4, 1, 50, w)), _num(r, "frames_days", 7, 0.1, 365, w),
        _num(r, "verify_min_ncc", 0.35, -1, 1, w), str(projection), _num(r, "panorama_ppd", 8, 2, 16, w),
        fov, sched, frigate)


def parse(raw: Any) -> AssistConfig:
    top = _only(raw, {"enabled", "osprey", "listen", "storage", "providers", "tasks", "capabilities",
                      "guardrails", "notify", "privacy", "survey"}, "config")
    enabled = _flag(top, "enabled", False, "config")
    o = _only(top.get("osprey"), {"url", "token_file", "cameras"}, "osprey")
    url = _text(o, "url", OspreyLink.url, "osprey") or OspreyLink.url
    if not url.startswith(("http://", "https://")):
        raise ValueError('"osprey.url" must be an http or https URL')
    cams = o.get("cameras", list(OspreyLink.cameras))
    if not isinstance(cams, list) or not cams or not all(isinstance(c, str) and c for c in cams):
        raise ValueError('"osprey.cameras" must be a list of camera ids')
    link = OspreyLink(url.rstrip("/"), _text(o, "token_file", OspreyLink.token_file, "osprey") or OspreyLink.token_file,
                      tuple(cams))
    ls = _only(top.get("listen"), {"bind", "port"}, "listen")
    port = _num(ls, "port", 8090, 1, 65535, "listen")
    listen = Listen(_text(ls, "bind", "127.0.0.1", "listen") or "127.0.0.1", int(port))
    st = _only(top.get("storage"), {"dir", "frame_store_px", "frames_days", "followup_frames_days", "samples_days",
                                    "decisions_days", "max_frames_mb"}, "storage")
    storage = Storage(
        _text(st, "dir", Storage.dir, "storage") or Storage.dir,
        int(_num(st, "frame_store_px", 1280, 320, 4096, "storage")),
        _num(st, "frames_days", 14, 0.1, 3650, "storage"),
        _num(st, "followup_frames_days", 3, 0.1, 3650, "storage"),
        _num(st, "samples_days", 90, 1, 3650, "storage"),
        _num(st, "decisions_days", 365, 1, 3650, "storage"),
        int(_num(st, "max_frames_mb", 2048, 16, 1_000_000, "storage")),
    )
    providers: dict[str, ProviderConfig] = {}
    given = top.get("providers") or {}
    if not isinstance(given, dict):
        raise ValueError('"providers" must be an object')
    for name, item in given.items():
        providers[str(name)] = parse_provider(str(name), item)
    tasks = {name: TaskConfig() for name in TASKS}
    given_tasks = top.get("tasks") or {}
    if not isinstance(given_tasks, dict):
        raise ValueError('"tasks" must be an object')
    for name, item in given_tasks.items():
        if name not in TASKS:
            raise ValueError(f'unknown task "{name}"; the tasks are {", ".join(TASKS)}')
        tasks[name] = parse_task(name, item)
    for name, task in tasks.items():
        if task.provider is not None and task.provider not in providers:
            raise ValueError(f'task "{name}" names provider "{task.provider}", which is not defined')
    caps = top.get("capabilities") or {}
    if not isinstance(caps, dict):
        raise ValueError('"capabilities" must be an object')
    unknown = set(caps) - set(CAPABILITIES)
    if unknown:
        raise ValueError(f'unknown capability: {", ".join(sorted(unknown))}')
    modes = {"quality": "suggest", "patrol": "off", "followup": "off", "presets": "off", "survey": "suggest"}
    for cap, body in caps.items():
        if cap == "quality":
            modes[cap] = _mode(body.get("mode", "suggest") if isinstance(body, dict) else None, "capabilities.quality.mode")
        else:
            b = _only(body, {"mode"}, f"capabilities.{cap}")
            modes[cap] = _mode(b.get("mode", "off"), f"capabilities.{cap}.mode")
    qraw = _only(caps.get("quality"), {"mode", "problems", "levers", "limits", "periodic_min", "settle_sec",
                                      "isp_settle_sec", "verify_after_sec", "min_gain", "max_regress",
                                      "fire_thresholds"}, "capabilities.quality")
    problems = tuple(qraw.get("problems", Quality.problems))
    if not problems or not set(problems) <= set(PROBLEMS):
        raise ValueError(f'"capabilities.quality.problems" may list {", ".join(PROBLEMS)}')
    levers = tuple(qraw.get("levers", Quality.levers))
    limits = qraw.get("limits", {})
    if not isinstance(limits, dict) or not all(isinstance(v, dict) for v in limits.values()):
        raise ValueError('"capabilities.quality.limits" must map a lever to {lo, hi, step, default}')
    build_levers(list(levers), {k: {a: float(b) for a, b in v.items()} for k, v in limits.items()})  # validates
    verify = qraw.get("verify_after_sec", list(Quality.verify_after_sec))
    if not isinstance(verify, list) or not verify or not all(isinstance(v, (int, float)) and 1 <= v <= 86400 for v in verify):
        raise ValueError('"capabilities.quality.verify_after_sec" must be a list of seconds')
    fire = qraw.get("fire_thresholds", {})
    if not isinstance(fire, dict) or not set(fire) <= set(PROBLEMS):
        raise ValueError('"capabilities.quality.fire_thresholds" keys must be problems')
    quality = Quality(
        modes["quality"], problems, levers, {k: {a: float(b) for a, b in v.items()} for k, v in limits.items()},
        _num(qraw, "periodic_min", 10, 1, 1440, "capabilities.quality"),
        _num(qraw, "settle_sec", 3, 0, 60, "capabilities.quality"),
        _num(qraw, "isp_settle_sec", 4, 0, 60, "capabilities.quality"),
        tuple(float(v) for v in verify),
        _num(qraw, "min_gain", 0.10, 0, 1, "capabilities.quality"),
        _num(qraw, "max_regress", 0.10, 0, 1, "capabilities.quality"),
        {k: float(v) for k, v in fire.items()},
    )
    gr = _only(top.get("guardrails"), {"moves_per_hour", "isp_writes_per_hour", "trials_per_hour",
                                       "probe_sessions_per_night", "max_probe_offsets", "max_offset_frac",
                                       "preset_changes_per_day", "structural_in_auto", "tracking_hold_sec",
                                       "pending_expire_hours", "quiet_hours", "quiet_isp"}, "guardrails")
    g = "guardrails"
    guardrails = Guardrails(
        int(_num(gr, "moves_per_hour", 6, 0, 600, g)), int(_num(gr, "isp_writes_per_hour", 6, 0, 600, g)),
        int(_num(gr, "trials_per_hour", 4, 0, 600, g)), int(_num(gr, "probe_sessions_per_night", 2, 0, 50, g)),
        int(_num(gr, "max_probe_offsets", 8, 1, 24, g)), _num(gr, "max_offset_frac", 0.04, 0.005, 0.10, g),
        int(_num(gr, "preset_changes_per_day", 2, 0, 50, g)), _flag(gr, "structural_in_auto", True, g),
        _num(gr, "tracking_hold_sec", 15, 1, 3600, g), _num(gr, "pending_expire_hours", 72, 1, 8760, g),
        _hhmm(gr.get("quiet_hours", []), "guardrails.quiet_hours"), _flag(gr, "quiet_isp", False, g),
    )
    pv = _only(top.get("privacy"), {"redact_names", "debug_prompts"}, "privacy")
    privacy = Privacy(_flag(pv, "redact_names", False, "privacy"), _flag(pv, "debug_prompts", False, "privacy"))
    for name in SURVEY_TASKS:
        task = tasks[name]
        if task.mode != "rules" and not task.send_frames:
            raise ValueError(f'task "{name}" needs "send_frames": true: a model cannot judge a picture it is not sent')
    return AssistConfig(enabled, link, listen, storage, providers, tasks, quality, modes, guardrails,
                        parse_notify(top.get("notify")), privacy, parse_survey(top.get("survey")))


def load(path: str | os.PathLike[str] | None = None) -> AssistConfig:
    target = Path(path or os.environ.get("OSPREY_ASSIST_CONFIG") or DEFAULT_PATH)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AssistConfig()
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {target}: {exc}") from exc
    return parse(raw)


__all__ = ["AssistConfig", "CAPABILITIES", "DEFAULT_LEVERS", "IMPLEMENTED", "TASKS", "load", "parse"]
