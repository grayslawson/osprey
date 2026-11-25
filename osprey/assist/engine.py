"""The Assist engine: sample, detect, decide, try, apply, verify, revert, learn.

Every camera write goes through ``Engine._gate`` first, and every change is recorded with
what it replaced so it can be undone. The engine never retries a refused move in a loop: it
gives up until the next trigger.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from . import metrics as metrics_mod
from .client import Camera, OspreyError
from .config import AssistConfig
from .decide import Decider, DiagnoseContext, JudgeContext
from .detect import Baselines, Detector
from .levers import GUARDS, TARGET, Lever, build_levers, candidates, next_value, ranges
from .signals import Signals
from .store import Store
from .survey_run import SurveyService
from .types import Action, Diagnosis, Finding, Metrics, Verdict

log = logging.getLogger("osprey.assist")

RULES_MIN_CONFIDENCE: Final = 0.5
HOLD_SEC: Final = 45.0           # one trial step; renewed while a longer sequence runs
POLL_SEC: Final = 3.0
ARRIVAL_GAP_SEC: Final = 300.0   # at most one arrival check per preset in this time
SCENE_HOLD_SEC: Final = 120.0    # a day/night change must hold this long, as in the patrol
PARK_TOLERANCE: Final = 0.012    # normalised distance that still counts as "at that preset"
# A guard metric must also move by at least this much before it counts as worse: small numbers jitter.
GUARD_FLOOR: Final = {"noise": 0.5, "luma": 8.0, "clip_hi": 0.02, "clip_lo": 0.02, "sharp": 10.0}
STATUSES_OPEN: Final = ("pending", "applied")
FRIGATE_WARNING: Final = ("Frigate zones and masks drawn for the day view will sit a little off at night. "
                          "Frigate caches presets: restart it or re-save its ONVIF settings to see the new one.")


class Refused(Exception):
    """The engine will not act now. The message says why in plain words."""


def _plan_moves(steps: list[dict[str, Any]]) -> bool:
    """Does this plan turn the camera? A night view or a zoom nudge does; a plain setting change does not.

    Quiet hours stop moves. They stop setting changes too, but only when ``guardrails.quiet_isp`` is set.
    """
    return any(step.get("lever") in ("night_view", "zoom_nudge") for step in steps)


@dataclass
class _Cam:
    id: str
    camera: Camera
    lock: threading.RLock = field(default_factory=threading.RLock)
    detectors: dict[tuple[str, str], Detector] = field(default_factory=dict)
    last_moves: int | None = None
    last_scene: str | None = None
    scene_since: float = 0.0
    arrival_at: float | None = None
    last_check: float = 0.0
    last_arrival_check: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    survey: bool = False   # a survey owns the camera: quality checks wait


class Engine:
    def __init__(self, cfg: AssistConfig, store: Store, cameras: dict[str, Camera], decider: Decider,
                 *, notifier: Any = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, signals: Signals | None = None) -> None:
        self.cfg, self.store, self.decider, self.notifier = cfg, store, decider, notifier
        self.signals = signals
        self.clock, self.sleep = clock, sleep
        self.cams = {cid: _Cam(cid, cam) for cid, cam in cameras.items()}
        self.levers: dict[str, Lever] = build_levers(list(cfg.quality.levers), cfg.quality.limits)
        self.book = Baselines(store)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cleanup = 0.0
        self.version = ""
        self.survey = SurveyService(self)

    # ------------------------------------------------------------ settings
    @property
    def enabled(self) -> bool:
        return bool(self.store.get("enabled", self.cfg.enabled))

    def mode(self, capability: str = "quality") -> str:
        return str(self.store.get(f"mode:{capability}", self.cfg.modes.get(capability, "off")))

    def set_enabled(self, value: bool) -> None:
        self.store.put("enabled", bool(value))
        if not value:
            self._kill()
        self._announce()

    def set_mode(self, capability: str, mode: str) -> None:
        if capability not in self.cfg.modes:
            raise Refused(f"unknown capability {capability}")
        self.store.put(f"mode:{capability}", mode)
        self._announce()

    def _kill(self) -> None:
        """Stop acting: release the patrol and put profile-driven settings back to their home values."""
        self.survey.cancel()
        for cs in self.cams.values():
            with cs.lock:
                try:
                    cs.camera.release()
                    home = self.store.get(f"home:{cs.id}", {})
                    if home:
                        self._write_isp(cs, {k: v for k, v in home.items()})
                except OspreyError as exc:
                    log.warning("kill switch: %s", exc)

    # --------------------------------------------------------------- loop
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="assist", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        self.reconcile()
        self.survey.reconcile()
        while not self._stop.wait(POLL_SEC):
            for cs in self.cams.values():
                try:
                    self.tick(cs.id)
                except OspreyError as exc:
                    cs.reason = f"Osprey: {exc.code}"
                except Exception:  # noqa: BLE001 - the loop must survive anything
                    log.exception("assist tick failed")
            self.housekeeping()

    def housekeeping(self) -> None:
        now = self.clock()
        self.expire_pending()
        self.survey.housekeeping()
        if now - self._last_cleanup > 3600:
            self._last_cleanup = now
            s = self.cfg.storage
            self.store.cleanup(frames_days=s.frames_days, followup_days=s.followup_frames_days,
                               samples_days=s.samples_days, decisions_days=s.decisions_days,
                               max_frames_mb=s.max_frames_mb)

    def reconcile(self) -> None:
        """After a restart: a decision still 'applied' with a verification owing is checked at the next visit.

        An applied change that has been waiting longer than a day is judged kept only if nothing
        contradicts it; otherwise it is reverted. No hold or dwell outlives the process (they expire).
        """
        for row in self.store.decisions(statuses=("applied",), limit=200):
            if self.clock() - row["ts"] > 86400:
                self._finish(row["id"], keep=True, reason="verified late, no contrary evidence")

    # ---------------------------------------------------------------- tick
    def tick(self, cam_id: str) -> None:
        cs = self.cams[cam_id]
        if not self.enabled or cs.survey or self.survey.busy:
            return
        with cs.lock:
            state = cs.camera.state()
            here = (state.get("ptz") or {}).get("position")
            if here:  # where the camera is, so a Frigate event can be placed on the survey map later
                self.survey.db.log_position(cam_id, float(here["pan"]), float(here["tilt"]), float(here["zoom"]))
            if self.mode() == "off":
                return
            signals = state.get("assist_signals") or {}
            ptz = state.get("ptz") or {}
            now = self.clock()
            scene = str(signals.get("scene") or ptz.get("scene") or "DAY").upper()
            if cs.last_scene is None:
                cs.last_scene, cs.scene_since = scene, now
            elif scene != cs.last_scene:
                cs.last_scene, cs.scene_since = scene, now
                cs.arrival_at = now  # a new scene is like arriving again: profile, then check
            sentry = cs.camera.sentry()
            moves = (sentry.get("state") or {}).get("moves")
            if moves is not None and moves != cs.last_moves:
                cs.last_moves, cs.arrival_at = int(moves), now
            idle = ptz.get("status") == "IDLE"
            settled_at = cs.arrival_at
            if settled_at is not None and idle and now - settled_at >= self.cfg.quality.settle_sec \
                    and now - cs.scene_since >= 0:
                cs.arrival_at = None
                self._arrived(cs, state, sentry, scene)
                return
            if idle and now - cs.last_check >= self.cfg.quality.periodic_min * 60 \
                    and not (sentry.get("state") or {}).get("enabled"):
                self.check(cam_id, "periodic")

    def _arrived(self, cs: _Cam, state: dict[str, Any], sentry: dict[str, Any], scene: str) -> None:
        preset = self._preset_name(state, sentry)
        if self._gate(cs, state) is None:
            self._apply_profile(cs, preset, scene)
        self.verify_visit(cs.id, preset, scene)
        last = cs.last_arrival_check.get(preset, 0.0)
        if self.clock() - last >= ARRIVAL_GAP_SEC:
            cs.last_arrival_check[preset] = self.clock()
            self.check(cs.id, "arrival")

    def _preset_name(self, state: dict[str, Any], sentry: dict[str, Any]) -> str:
        point = (sentry.get("state") or {}).get("point")
        if point:
            return str(point)
        here = (state.get("ptz") or {}).get("position") or {}
        for preset in state.get("presets") or []:
            pos = preset.get("position") or {}
            if all(abs(float(pos.get(k, 9)) - float(here.get(k, -9))) <= PARK_TOLERANCE for k in ("pan", "tilt", "zoom")):
                return str(preset.get("name"))
        return "(free)"

    # ---------------------------------------------------------------- gate
    def _gate(self, cs: _Cam, state: dict[str, Any], *, moves: bool = False, write: bool = False,
              quiet: bool = True) -> str | None:
        """Why Assist must stand down right now, or None. Frigate and people always win.

        ``moves`` is a call that turns the camera, ``write`` a call that only changes a setting.
        Quiet hours stop moves; they stop setting changes too when ``guardrails.quiet_isp`` is set.
        """
        sig = state.get("assist_signals") or {}
        age = sig.get("auto_active_age_sec")
        if age is not None and float(age) < self.cfg.guardrails.tracking_hold_sec:
            return "Frigate is tracking"
        if float(sig.get("manual_hold_remaining_sec") or 0) > 0:
            return "a person is controlling the camera"
        owner = sig.get("lease_owner")
        if owner not in (None, "assist"):
            return f"the camera is in use ({owner})"
        if (state.get("ptz") or {}).get("status") not in ("IDLE", None) and (moves or write):
            return "the camera is moving"
        if quiet and self._quiet():
            if moves:
                return "quiet hours"
            if write and self.cfg.guardrails.quiet_isp:
                return "quiet hours"
        return None

    def _quiet(self) -> bool:
        t = time.localtime(self.clock())
        minutes = t.tm_hour * 60 + t.tm_min
        for item in self.cfg.guardrails.quiet_hours:
            a = int(item[:2]) * 60 + int(item[3:5])
            b = int(item[6:8]) * 60 + int(item[9:11])
            if (a <= minutes < b) if a <= b else (minutes >= a or minutes < b):
                return True
        return False

    def _budget(self, cs: _Cam, kind: str) -> bool:
        g = self.cfg.guardrails
        limit, window = {"move": (g.moves_per_hour, 3600), "isp": (g.isp_writes_per_hour, 3600),
                         "trial": (g.trials_per_hour, 3600), "probe": (g.probe_sessions_per_night, 43200),
                         "preset": (g.preset_changes_per_day, 86400)}[kind]
        return self.store.spent(cs.id, kind, window) < limit

    def budgets(self, cam_id: str) -> dict[str, dict[str, int]]:
        g = self.cfg.guardrails
        out: dict[str, dict[str, int]] = {}
        for kind, name, limit, window in (("move", "moves", g.moves_per_hour, 3600),
                                          ("isp", "isp_writes", g.isp_writes_per_hour, 3600),
                                          ("trial", "trials", g.trials_per_hour, 3600),
                                          ("probe", "probes", g.probe_sessions_per_night, 43200),
                                          ("preset", "preset_changes", g.preset_changes_per_day, 86400)):
            out[name] = {"used": self.store.spent(cam_id, kind, window), "limit": limit}
        total = sum(p.budget.max_per_hour for p in self.decider.providers.values()) if self.decider.providers else 0
        out["model_calls"] = {"used": self.store.calls_today(), "limit": total}
        return out

    # --------------------------------------------------------------- shadow
    def _shadow(self, cs: _Cam) -> dict[str, float]:
        return {k: float(v) for k, v in self.store.get(f"shadow:{cs.id}", {}).items()}

    def _fit(self, jpeg: bytes) -> bytes:
        """A frame no wider than ``storage.frame_store_px`` before it goes on disk."""
        return metrics_mod.fit_width(jpeg, self.cfg.storage.frame_store_px)

    def _write_isp(self, cs: _Cam, settings: dict[str, float]) -> bool | None:
        if not settings:
            return True
        acked = cs.camera.isp({k: v for k, v in settings.items()})
        if acked is not False:
            shadow = self._shadow(cs)
            shadow.update(settings)
            self.store.put(f"shadow:{cs.id}", shadow)
        return acked

    def _apply_profile(self, cs: _Cam, preset: str, scene: str) -> None:
        """Arrive at a preset: make the ISP match its profile, and put other presets' settings back home."""
        profile = self.store.profile(cs.id, preset, scene)
        home: dict[str, float] = self.store.get(f"home:{cs.id}", {})
        shadow = self._shadow(cs)
        diff = {}
        for key in self.store.profile_keys(cs.id) | set(profile):
            want = profile.get(key, home.get(key))
            if want is not None and shadow.get(key) != want:
                diff[key] = want
        if diff:
            log.info("assist: profile for %s %s sets %s", preset, scene, sorted(diff))
            self._write_isp(cs, diff)

    # ---------------------------------------------------------------- check
    def check(self, cam_id: str, trigger: str = "manual") -> int | None:
        """Look at the current view once. Returns the decision id when one was made."""
        cs = self.cams[cam_id]
        if not self.enabled:
            raise Refused("Assist is switched off")
        mode = self.mode()
        if mode == "off":
            raise Refused("quality checks are off")
        if self.survey.busy:
            raise Refused("not now: a survey is running")
        with cs.lock:
            state = cs.camera.state()
            reason = self._gate(cs, state)
            if reason:
                cs.reason = reason
                if trigger == "manual":
                    raise Refused(f"not now: {reason}")
                return None
            cs.reason = ""
            sentry = cs.camera.sentry()
            scene = str((state.get("assist_signals") or {}).get("scene") or (state.get("ptz") or {}).get("scene") or "DAY").upper()
            preset = self._preset_name(state, sentry)
            frame = cs.camera.snapshot()
            try:
                metrics = metrics_mod.compute(frame)
            except ValueError as exc:
                log.warning("assist: unusable snapshot: %s", exc)
                return None
            cs.last_check = self.clock()
            hour = time.localtime(self.clock()).tm_hour
            z = self.book.z(cs.id, preset, scene, hour, metrics)
            detector = cs.detectors.setdefault((preset, scene), Detector())
            q = self.cfg.quality
            findings = detector.evaluate(metrics, z, problems=q.problems, scale=self._scale(cs, preset),
                                         fire=q.fire_thresholds)
            self.store.add_sample(cs.id, preset, scene, trigger, metrics.as_dict())
            if not findings:
                self.book.learn(cs.id, preset, scene, hour, metrics)
            night_presets = self._night_presets(cs)
            rules = self._rules_diagnosis(cs, preset, scene, findings, night_presets)
            task = self.decider.tasks.get("quality.diagnose")
            diag = rules or Diagnosis("ok", 1.0, "rules")
            by_model = self._by_signals(cs, preset, scene, rules, frame, night_presets) if self.signals is not None else None
            if by_model is not None:
                diag = by_model
            elif task is not None and task.mode != "rules":
                ctx = self._context(cs, preset, scene, metrics, z, findings, night_presets)
                diag = self.decider.diagnose(ctx, rules, frame)
            if diag.problem in ("ok", "unknown") or diag.problem not in q.problems:
                return None
            if any(d["preset"] == preset and d["scene"] == scene
                   for d in self.store.decisions(statuses=STATUSES_OPEN, camera=cs.id, limit=50)):
                return None  # one open suggestion or unverified change per view
            if self._muted(cs, preset, diag.problem):
                return self._record(cs, diag, preset, scene, trigger, metrics, frame, None, "observed",
                                    reason="muted after repeated 'not a problem' answers")
            return self._propose(cs, state, diag, preset, scene, trigger, metrics, frame, mode)

    def _by_signals(self, cs: _Cam, preset: str, scene: str, rules: Diagnosis | None, frame: bytes,
                    night_presets: set[str]) -> Diagnosis | None:
        """Glare and blur from their own models, with the rules deciding exposure and noise.

        None means no signal model answered, so the caller keeps the rules' view.
        """
        assert self.signals is not None
        glare, blur = self.signals.glare(frame), self.signals.blur(frame)
        if glare is None and blur is None:
            return None
        base = rules
        if rules is not None and ((rules.problem == "glare" and glare is not None)
                                  or (rules.problem == "blur" and blur is not None)):
            base = None  # a model answered the question the rule was guessing at
        hit = glare if glare is not None and glare.positive else None
        problem = "glare"
        if hit is None and blur is not None and blur.positive:
            hit, problem = blur, "blur"
        if hit is None:
            return base or Diagnosis("ok", 1.0, "model")
        ladder = self._ladder(cs, preset, scene, problem, night_presets)
        picked = self.signals.action(frame)
        if isinstance(picked, str):  # "none" or "wait": the action model sees nothing worth doing now
            actions: tuple[Action, ...] = ()
        else:
            match = [a for a in ladder if picked is not None and a.lever == picked[0] and (a.change >= 0) == (picked[1] > 0)]
            actions = tuple((match or list(ladder))[:1])
        return Diagnosis(problem, hit.confidence, f"model:{hit.model}",
                         f"{hit.model} says {problem} {hit.p:.2f} (needs {hit.threshold:g})", actions)

    def _scale(self, cs: _Cam, preset: str) -> dict[str, float]:
        return {p: float(self.store.get(f"scale:{cs.id}:{preset}:{p}", 1.0)) for p in ("blur", "glare", "under", "over", "noise")}

    def _muted(self, cs: _Cam, preset: str, problem: str) -> bool:
        until = float(self.store.get(f"mute_until:{cs.id}:{preset}:{problem}", 0.0))
        return bool(self.store.get(f"mute:{cs.id}:{preset}:{problem}", False) or self.store.get(f"mute:{cs.id}:{preset}:*", False)
                    or until > self.clock())

    def _night_presets(self, cs: _Cam) -> set[str]:
        cfg = (cs.camera.sentry().get("config") or {}).get("points") or []
        return {str(p["night_preset"]).casefold() for p in cfg if p.get("night_preset")}

    def _rules_diagnosis(self, cs: _Cam, preset: str, scene: str, findings: list[Finding],
                         night_presets: set[str]) -> Diagnosis | None:
        if not findings:
            return None
        top = findings[0]
        actions = self._ladder(cs, preset, scene, top.problem, night_presets)
        evidence = f"{top.problem} {top.value:g} ({top.z:+.1f} from this view's normal)"
        return Diagnosis(top.problem, top.confidence, "rules", evidence, actions[:1])

    def _ladder(self, cs: _Cam, preset: str, scene: str, problem: str, night_presets: set[str]) -> tuple[Action, ...]:
        blocked = set(self.store.blocked_levers(cs.id, preset, scene, problem))
        if preset.casefold() in night_presets or preset == "(free)":
            blocked.add("night_view")  # a night view of a night view, or a view with no patrol point
        steps = candidates(problem, scene, self.levers, frozenset(blocked))
        shadow = self._shadow(cs)
        usable: list[Action] = []
        for act in steps:
            lever = self.levers[act.lever]
            if lever.kind == "isp" and next_value(lever, shadow, act.change) is None:
                continue
            usable.append(act)

        def rank(a: Action) -> float:
            st = self.store.lever(cs.id, preset, scene, problem, a.lever)
            return float(-(st["good"] + 1) / (st["good"] + st["bad"] + 2))

        return tuple(sorted(usable, key=rank)) if len(usable) > 1 else tuple(usable)

    def _context(self, cs: _Cam, preset: str, scene: str, metrics: Metrics, z: dict[str, float],
                 findings: list[Finding], night_presets: set[str]) -> DiagnoseContext:
        cands = {p: self._ladder(cs, preset, scene, p, night_presets) for p in self.cfg.quality.problems}
        similar: list[str] = []
        for p in self.cfg.quality.problems:
            for fb in self.store.feedback_for(cs.id, preset, p, 2):
                similar.append(f"{fb['verdict']} ({fb['reason'] or 'ok'}): {p}, {fb['change']}")
        recent = tuple(f"{d['problem']}: {d.get('change', '')} -> {d['status']}"
                       for d in self.store.decisions(camera=cs.id, limit=3) if d["preset"] == preset)
        return DiagnoseContext(cs.id, preset, scene, time.strftime("%H:%M", time.localtime(self.clock())), metrics, z,
                               tuple(findings), cands, ranges(self.levers), recent, tuple(similar[:3]))

    # ------------------------------------------------------------- propose
    def _propose(self, cs: _Cam, state: dict[str, Any], diag: Diagnosis, preset: str, scene: str, trigger: str,
                 metrics: Metrics, frame: bytes, mode: str) -> int | None:
        if not diag.actions or diag.ask_human and mode != "observe":
            return self._record(cs, diag, preset, scene, trigger, metrics, frame, None, "observed",
                                reason="no safe action was found" if not diag.actions else "the model asked for a person")
        if diag.confidence < RULES_MIN_CONFIDENCE:
            return self._record(cs, diag, preset, scene, trigger, metrics, frame, None, "observed", reason="low confidence")
        structural = any(self.levers[a.lever].kind == "structural" for a in diag.actions if a.lever in self.levers)
        if mode == "observe":
            return self._record(cs, diag, preset, scene, trigger, metrics, frame, None, "observed")
        frozen = any(self.store.lever(cs.id, preset, scene, diag.problem, a.lever)["frozen"] for a in diag.actions)
        direct = mode == "auto" and (not structural or self.cfg.guardrails.structural_in_auto)
        direct = direct or (mode == "act" and not structural)
        if frozen:
            direct = False
        try:
            if direct:
                row = self._record(cs, diag, preset, scene, trigger, metrics, frame, None, "approved", structural=structural)
                try:
                    self._apply(cs, row, "assist", state)
                except (Refused, OspreyError) as exc:
                    self.store.update_decision(row, status="failed", resolved_at=self.clock(), body={"reason": str(exc)})
                    raise
                return row
            return self._trial(cs, state, diag, preset, scene, trigger, metrics, frame, structural)
        except (Refused, OspreyError) as exc:
            log.info("assist: %s", exc)
            return None

    def _expiry(self) -> float:
        return self.clock() + self.cfg.guardrails.pending_expire_hours * 3600

    def _record(self, cs: _Cam, diag: Diagnosis, preset: str, scene: str, trigger: str, metrics: Metrics,
                frame: bytes | None, after: tuple[bytes, Metrics] | None, status: str, *, structural: bool = False,
                reason: str = "", plan: dict[str, Any] | None = None) -> int:
        before_name = self.store.save_frame(self._fit(frame)) if frame else None
        after_name = self.store.save_frame(self._fit(after[0])) if after else None
        plan = plan or self._plan(cs, diag, preset, scene)
        body: dict[str, Any] = {
            "summary": self._summary(diag, preset, scene), "change": plan["change"], "expect": plan["expect"],
            "warning": FRIGATE_WARNING if structural else None, "plan": plan, "reason": reason,
            "metrics": metrics.as_dict(), "glare_map": list(metrics.glare_map) or None,
            "model_json": {"problem": diag.problem, "confidence": diag.confidence, "source": diag.source,
                           "evidence": diag.evidence, "disagree": diag.disagree},
            "outcome": None, "after_metrics": after[1].as_dict() if after else None,
        }
        row = {"camera": cs.id, "preset": preset, "scene": scene, "trigger": trigger, "problem": diag.problem,
               "confidence": diag.confidence, "source": diag.source, "status": status, "structural": structural,
               "expires_at": self._expiry() if status == "pending" else None, "before_frame": before_name,
               "after_frame": after_name}
        decision_id = self.store.add_decision(row, body)
        return decision_id

    def _plan(self, cs: _Cam, diag: Diagnosis, preset: str, scene: str) -> dict[str, Any]:
        shadow = self._shadow(cs)
        steps: list[dict[str, Any]] = []
        words: list[str] = []
        for act in diag.actions:
            lever = self.levers.get(act.lever)
            if lever is None:
                continue
            if lever.kind == "isp":
                new = next_value(lever, shadow, act.change)
                if new is None:
                    continue
                old = shadow.get(lever.key, lever.default)
                steps.append({"lever": act.lever, "key": lever.key, "from": old, "to": new, "assumed": lever.key not in shadow})
                words.append(f"{lever.key} {old:g} to {new:g}")
            elif act.lever == "zoom_nudge":
                steps.append({"lever": "zoom_nudge", "size": lever.default})
                words.append("a small zoom nudge to refocus")
            elif act.lever == "night_view":
                steps.append({"lever": "night_view", "name": f"{preset}Night", "point": preset})
                words.append(f"a new night view, {preset}Night")
            else:
                steps.append({"lever": act.lever, "change": act.change})
                words.append(act.lever)
        expect = {"glare": "Glare should drop.", "blur": "The picture should be sharper.", "under": "The picture should be brighter.",
                  "over": "The picture should be less bright.", "noise": "The picture should be cleaner."}.get(diag.problem, "")
        return {"steps": steps, "change": " and ".join(words) or "no change", "expect": expect}

    @staticmethod
    def _summary(diag: Diagnosis, preset: str, scene: str) -> str:
        name = {"glare": "Glare", "blur": "A soft picture", "under": "A dark picture", "over": "An over-bright picture",
                "noise": "A noisy picture"}.get(diag.problem, diag.problem)
        when = "at night" if scene == "NIGHT" else "by day"
        return f"{name} at {preset} {when}. {diag.evidence}".strip()

    # --------------------------------------------------------------- trials
    def _hold(self, cs: _Cam, reason: str = "assist-trial") -> None:
        try:
            cs.camera.hold(HOLD_SEC, reason)
        except OspreyError as exc:
            raise Refused(f"cannot pause the patrol: {exc.code}") from None

    def _trial(self, cs: _Cam, state: dict[str, Any], diag: Diagnosis, preset: str, scene: str, trigger: str,
               metrics: Metrics, frame: bytes, structural: bool) -> int:
        """Try the change to get an 'after' frame, put everything back, and file a suggestion."""
        if not self._budget(cs, "trial"):
            raise Refused("trial budget used up for this hour")
        plan = self._plan(cs, diag, preset, scene)
        steps = plan["steps"]
        moving = _plan_moves(steps)
        reason = self._gate(cs, state, moves=moving, write=not moving)
        if reason:
            raise Refused(reason)
        if not steps:
            raise Refused("nothing to change")
        self.store.spend(cs.id, "trial")
        after: tuple[bytes, Metrics] | None = None
        try:
            if steps[0]["lever"] == "night_view":
                found = self._search_night_view(cs, preset, metrics)
                if found is None:
                    return self._record(cs, diag, preset, scene, trigger, metrics, frame, None, "observed",
                                        structural=True, reason="no nearby view cut the glare enough")
                offset, after = found
                plan = {**plan, "steps": [{**steps[0], "pan": offset[0], "tilt": offset[1]}],
                        "change": f"New night view {preset}Night: pan {offset[0] * 100:+.1f}%, tilt {offset[1] * 100:+.1f}%"}
            else:
                after = self._try_steps(cs, steps)
        finally:
            self._safe_release(cs)
        row = self._record(cs, diag, preset, scene, trigger, metrics, frame, after, "pending", structural=structural, plan=plan)
        self._notify(self.store.decision(row), "suggested")
        return row

    def _safe_release(self, cs: _Cam) -> None:
        try:
            cs.camera.release()
        except OspreyError:
            pass  # the hold expires on its own

    def _try_steps(self, cs: _Cam, steps: list[dict[str, Any]]) -> tuple[bytes, Metrics] | None:
        """Apply, look, and restore. Zoom nudges are their own restore."""
        self._hold(cs)
        prior = self._shadow(cs)
        isp = {s["key"]: s["to"] for s in steps if "key" in s}
        if isp and self._write_isp(cs, isp) is False:
            raise Refused("the camera refused the setting")
        try:
            if any(s["lever"] == "zoom_nudge" for s in steps):
                self._nudge(cs, float(next(s["size"] for s in steps if s["lever"] == "zoom_nudge")))
            self.sleep(self.cfg.quality.isp_settle_sec)
            frame = cs.camera.snapshot()
            return frame, metrics_mod.compute(frame)
        finally:
            if isp:
                defaults = {k: self.levers[k].default for k in isp if k in self.levers}
                self._write_isp(cs, {k: prior.get(k, defaults.get(k, v)) for k, v in isp.items()})

    def _nudge(self, cs: _Cam, size: float) -> None:
        if not self._budget(cs, "move"):
            raise Refused("move budget used up for this hour")
        self.store.spend(cs.id, "move")
        cs.camera.move_relative(zoom=size)
        self.sleep(self.cfg.quality.settle_sec)
        cs.camera.move_relative(zoom=-size)
        self.sleep(self.cfg.quality.settle_sec)

    # --------------------------------------------------- night view search
    def _search_night_view(self, cs: _Cam, preset: str, before: Metrics) -> tuple[tuple[float, float], tuple[bytes, Metrics]] | None:
        """Probe small offsets around a preset and keep the smallest that cuts glare by half.

        shortcut: the 85% overlap rule is enforced by capping the offset, not by measuring the
        shift between frames; revisit with phase correlation if a model ever needs it.
        """
        if not self._budget(cs, "probe"):
            raise Refused("night probe budget used up")
        token = self._token_for(cs, preset)
        if token is None:
            raise Refused(f"{preset} is not a saved preset")
        self.store.spend(cs.id, "probe")
        g = self.cfg.guardrails
        steps = [0.01, 0.02, 0.03, 0.04]
        offsets: list[tuple[float, float]] = []
        for size in steps:
            if size <= g.max_offset_frac + 1e-9:
                offsets += [(size, 0.0), (-size, 0.0), (0.0, size), (0.0, -size)]
        offsets = offsets[: g.max_probe_offsets]
        self._hold(cs, "assist-night-probe")
        try:
            for offset in offsets:
                state = cs.camera.state()
                reason = self._gate(cs, state, moves=False)
                if reason:
                    raise Refused(reason)  # Frigate or a person arrived: leave the camera where they put it
                cs.camera.hold(HOLD_SEC, "assist-night-probe")
                self._go(cs, token, offset)
                frame = cs.camera.snapshot()
                m = metrics_mod.compute(frame)
                if m.glare <= before.glare * 0.5 and m.sharp >= before.sharp * 0.7:
                    self._go(cs, token, (0.0, 0.0))
                    return offset, (frame, m)
            self._go(cs, token, (0.0, 0.0))
            return None
        finally:
            self._safe_release(cs)

    def _go(self, cs: _Cam, token: int, offset: tuple[float, float]) -> None:
        cs.camera.goto(token)
        self.sleep(self.cfg.quality.settle_sec + 1)
        if offset != (0.0, 0.0):
            cs.camera.move_relative(pan=offset[0], tilt=offset[1])
            self.sleep(self.cfg.quality.settle_sec + 1)

    def _token_for(self, cs: _Cam, name: str) -> int | None:
        for p in cs.camera.presets():
            if str(p.get("name", "")).casefold() == name.casefold():
                return int(p["token"])
        return None

    # --------------------------------------------------------------- apply
    def _apply(self, cs: _Cam, decision_id: int, principal: str, state: dict[str, Any] | None = None) -> None:
        row = self.store.decision(decision_id)
        if row is None:
            raise Refused("no such decision")
        state = state or cs.camera.state()
        plan = row["plan"]
        steps = plan["steps"]
        moving = _plan_moves(steps)
        reason = self._gate(cs, state, moves=moving, write=not moving)
        if reason:
            raise Refused(reason)
        problem = row["problem"]
        prior = self._shadow(cs)
        try:
            self._hold(cs, "assist-apply")
            if steps and steps[0]["lever"] == "night_view":
                applied = self._apply_night_view(cs, row, steps[0])
            else:
                applied = self._apply_isp(cs, steps, prior)
            self.sleep(self.cfg.quality.isp_settle_sec)
            frame = cs.camera.snapshot()
            m = metrics_mod.compute(frame)
        finally:
            self._safe_release(cs)
        after = self.store.save_frame(self._fit(frame))
        due = [self.clock() + s for s in self.cfg.quality.verify_after_sec]
        self.store.update_decision(decision_id, status="applied", after_frame=after, expires_at=None,
                                   body={"applied": applied, "applied_by": principal, "verify_due": due,
                                         "after_metrics": m.as_dict(), "applied_at": self.clock()})
        self.store.add_feedback(decision_id, principal, "apply")
        updated = self.store.decision(decision_id)
        self._notify(updated, "applied")
        target, lower = TARGET[problem]
        before_v = float(row["metrics"][target])
        gain = self._gain(before_v, float(getattr(m, target)), lower)
        if gain < -self.cfg.quality.max_regress:  # clearly worse right away: do not wait
            self.revert(decision_id, "worse straight away", principal="assist")

    def _apply_isp(self, cs: _Cam, steps: list[dict[str, Any]], prior: dict[str, float]) -> dict[str, Any]:
        wanted = {s["key"]: s["to"] for s in steps if "key" in s}
        prev = {s["key"]: prior.get(s["key"], s["from"]) for s in steps if "key" in s}
        if wanted:
            if not self._budget(cs, "isp"):
                raise Refused("setting budget used up for this hour")
            self.store.spend(cs.id, "isp")
            if self._write_isp(cs, wanted) is False:
                raise Refused("the camera refused the setting")
        if any(s["lever"] == "zoom_nudge" for s in steps):
            self._nudge(cs, float(next(s["size"] for s in steps if s["lever"] == "zoom_nudge")))
        return {"isp": wanted, "prior": prev, "nudged": any(s["lever"] == "zoom_nudge" for s in steps)}

    def _apply_night_view(self, cs: _Cam, row: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if not self._budget(cs, "preset"):
            raise Refused("preset change budget used up for today")
        token = self._token_for(cs, row["preset"])
        if token is None:
            raise Refused(f"{row['preset']} is no longer a saved preset")
        names = {str(p.get("name", "")).casefold() for p in cs.camera.presets()}
        name = str(step["name"])
        n = 2
        while name.casefold() in names:
            name, n = f"{step['name']}{n}", n + 1
        self._go(cs, token, (float(step["pan"]), float(step["tilt"])))
        self.store.spend(cs.id, "preset")
        saved = cs.camera.save_preset(name)
        cs.camera.set_night_view(str(step["point"]), name)
        cs.camera.goto(token)
        return {"night_view": name, "point": step["point"], "token": saved.get("token")}

    # -------------------------------------------------------------- verify
    @staticmethod
    def _gain(before: float, after: float, lower_is_better: bool) -> float:
        """Relative improvement of the target metric; negative means worse."""
        base = max(abs(before), 1e-3)
        return ((before - after) if lower_is_better else (after - before)) / base

    def verify_visit(self, cam_id: str, preset: str, scene: str) -> None:
        """On arrival at a preset, check any applied change here whose verification is due."""
        cs = self.cams[cam_id]
        for row in self.store.decisions(statuses=("applied",), camera=cam_id, limit=50):
            due = row.get("verify_due") or []
            here = {row["preset"], (row.get("applied") or {}).get("night_view")}
            if preset not in here or row["scene"] != scene or not due or self.clock() < due[0]:
                continue
            frame = cs.camera.snapshot()
            m = metrics_mod.compute(frame)
            verdict = self._judge(row, frame, m)
            target, lower = TARGET[row["problem"]]
            before_v = float(row["metrics"][target])
            gain = self._gain(before_v, float(getattr(m, target)), lower)
            guard_bad = any(
                self._gain(float(row["metrics"][g]), float(getattr(m, g)), g in ("noise", "clip_hi", "clip_lo"))
                < -self.cfg.quality.max_regress
                and abs(float(getattr(m, g)) - float(row["metrics"][g])) > GUARD_FLOOR[g]
                for g in GUARDS[row["problem"]])
            rest = due[1:]
            worse = verdict is not None and verdict.better == "before" and verdict.confidence >= 0.7
            if gain < self.cfg.quality.min_gain or guard_bad or worse:
                why = "judged worse" if worse else "a guard metric got worse" if guard_bad else "not better enough"
                self.revert(row["id"], why, principal="assist", outcome={"verdict": "worse" if worse or guard_bad else "same",
                                                                         "target_before": before_v, "target_after": float(getattr(m, target))})
            elif not rest:
                self._finish(row["id"], keep=True, reason="held up", outcome={
                    "verdict": "better", "target_before": before_v, "target_after": float(getattr(m, target))})
            else:
                self.store.update_decision(row["id"], body={"verify_due": rest})

    def _judge(self, row: dict[str, Any], frame: bytes, m: Metrics) -> Verdict | None:
        before = self.store.frame(row["before_frame"]) if row.get("before_frame") else None
        if self.signals is not None and before is not None:
            got = self.signals.judge(row["problem"], before, frame)
            if got is not None:
                return Verdict(got[0], got[1], "model", got[2])
        task = self.decider.tasks.get("quality.judge")
        if task is None or task.mode == "rules":
            return None
        ctx = JudgeContext(row["preset"], row["scene"], row["problem"], TARGET[row["problem"]][0],
                           _metrics_from(row["metrics"]), m)
        return self.decider.judge(ctx, before, frame, None)

    def _finish(self, decision_id: int, *, keep: bool, reason: str, outcome: dict[str, Any] | None = None) -> None:
        row = self.store.decision(decision_id)
        if row is None or row["status"] != "applied":
            return
        cs = self.cams.get(row["camera"])
        self.store.update_decision(decision_id, status="kept", resolved_at=self.clock(),
                                   body={"reason": reason, "outcome": outcome, "verify_due": []})
        if cs is not None:
            self._learn_lever(cs, row, good=True)
            applied = row.get("applied") or {}
            if applied.get("isp"):
                self._profile_set(cs, row, applied["isp"], applied.get("prior") or {})
        self._notify(self.store.decision(decision_id), "kept")

    def _profile_set(self, cs: _Cam, row: dict[str, Any], values: dict[str, float], prior: dict[str, float]) -> None:
        profile = self.store.profile(cs.id, row["preset"], row["scene"])
        profile.update(values)
        self.store.set_profile(cs.id, row["preset"], row["scene"], profile)
        home = self.store.get(f"home:{cs.id}", {})
        for k, v in prior.items():
            home.setdefault(k, v)
        self.store.put(f"home:{cs.id}", home)

    def _learn_lever(self, cs: _Cam, row: dict[str, Any], *, good: bool, cooldown_sec: float = 86400) -> None:
        for step in row["plan"]["steps"]:
            st = self.store.lever(cs.id, row["preset"], row["scene"], row["problem"], step["lever"])
            st["good" if good else "bad"] += 1
            if not good:
                st["cooldown_until"] = self.clock() + cooldown_sec
            direction = 1 if (step.get("to", 0) - step.get("from", 0)) >= 0 else -1
            if st["last_dir"] and direction != st["last_dir"]:
                st["flips"] = [t for t in st["flips"] if self.clock() - t < 6 * 3600] + [self.clock()]
                if len(st["flips"]) >= 2:
                    st["frozen"] = True
            st["last_dir"] = direction
            self.store.save_lever(cs.id, row["preset"], row["scene"], row["problem"], step["lever"], st)

    # -------------------------------------------------------------- revert
    def revert(self, decision_id: int, reason: str, *, principal: str, outcome: dict[str, Any] | None = None,
               status: str = "reverted") -> dict[str, Any]:
        row = self.store.decision(decision_id)
        if row is None:
            raise Refused("no such decision")
        if row["status"] not in ("applied", "kept"):
            raise Refused("this change is not in effect")
        cs = self.cams[row["camera"]]
        applied = row.get("applied") or {}
        with cs.lock:
            if applied.get("isp") and applied.get("prior"):
                self._write_isp(cs, {k: float(v) for k, v in applied["prior"].items()})
                profile = self.store.profile(cs.id, row["preset"], row["scene"])
                for k in applied["isp"]:
                    profile.pop(k, None)
                self.store.set_profile(cs.id, row["preset"], row["scene"], profile)
            if applied.get("night_view"):
                try:
                    cs.camera.set_night_view(str(applied["point"]), None)
                except OspreyError as exc:
                    raise Refused(f"cannot clear the night view: {exc.code}") from None
        cooldown = 7 * 86400 if principal != "assist" else 86400
        self._learn_lever(cs, row, good=False, cooldown_sec=cooldown)
        self.store.update_decision(decision_id, status=status, resolved_at=self.clock(),
                                   body={"reason": reason, "outcome": outcome, "verify_due": []})
        out = self.store.decision(decision_id) or row
        self._notify(out, "reverted")
        return out

    # ----------------------------------------------------------- approvals
    def approve(self, decision_id: int, principal: str) -> dict[str, Any]:
        row = self._pending(decision_id)
        cs = self.cams[row["camera"]]
        with cs.lock:
            if not self.enabled:
                raise Refused("Assist is switched off")
            self._apply(cs, decision_id, principal)
            self._learn_feedback(cs, row, "approve", "")
        return self.store.decision(decision_id) or row

    def reject(self, decision_id: int, principal: str, reason: str = "", note: str = "") -> dict[str, Any]:
        row = self._pending(decision_id)
        cs = self.cams[row["camera"]]
        self.store.update_decision(decision_id, status="rejected", resolved_at=self.clock(), body={"reason": reason or "rejected"})
        self.store.add_feedback(decision_id, principal, "reject", reason, note)
        self._learn_feedback(cs, row, "reject", reason)
        out = self.store.decision(decision_id) or row
        self._notify(out, "rejected")
        return out

    def snooze(self, decision_id: int, principal: str) -> dict[str, Any]:
        row = self._pending(decision_id)
        self.store.update_decision(decision_id, status="snoozed", resolved_at=self.clock())
        self.store.add_feedback(decision_id, principal, "snooze")
        cs = self.cams[row["camera"]]
        self.store.put(f"mute_until:{cs.id}:{row['preset']}:{row['problem']}", self.clock() + 86400)
        return self.store.decision(decision_id) or row

    def undo(self, decision_id: int, principal: str) -> dict[str, Any]:
        row = self.store.decision(decision_id)
        if row is None or row["status"] not in ("applied", "kept"):
            raise Refused("this change is not in effect")
        self.store.add_feedback(decision_id, principal, "undo")
        cs = self.cams[row["camera"]]
        self._learn_feedback(cs, row, "reject", "looks_worse")
        return self.revert(decision_id, f"undone by {principal}", principal=principal, status="undone")

    def _pending(self, decision_id: int) -> dict[str, Any]:
        row = self.store.decision(decision_id)
        if row is None:
            raise Refused("no such decision")
        if row["status"] != "pending":
            raise Refused(f"this suggestion is already {row['status']}")
        return row

    def _learn_feedback(self, cs: _Cam, row: dict[str, Any], verdict: str, reason: str) -> None:
        key = f"scale:{cs.id}:{row['preset']}:{row['problem']}"
        scale = float(self.store.get(key, 1.0))
        if verdict == "approve":
            self.store.put(key, max(0.75, scale * 0.9))
        elif reason == "not_a_problem":
            self.store.put(key, min(2.0, scale * 1.25))
            if self.store.reject_count(cs.id, row["preset"], row["problem"]) >= 5:
                self.store.put(f"mute:{cs.id}:{row['preset']}:{row['problem']}", True)
        elif reason == "dont_touch":
            self.store.put(f"mute:{cs.id}:{row['preset']}:*", True)
        elif reason in ("wrong_fix", "looks_worse"):
            self._learn_lever(cs, row, good=False)

    def expire_pending(self) -> None:
        now = self.clock()
        for row in self.store.decisions(statuses=("pending",), limit=200):
            if row["expires_at"] and row["expires_at"] < now:
                self.store.update_decision(row["id"], status="expired", resolved_at=now)
                self._notify(self.store.decision(row["id"]), "expired")

    # --------------------------------------------------------------- views
    def view(self, row: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "ts", "camera", "capability", "preset", "scene", "trigger", "problem", "confidence", "source",
                "summary", "change", "expect", "warning", "structural", "status", "before_frame", "after_frame",
                "glare_map", "outcome", "reason", "expires_at", "model_json", "metrics")
        return {k: row.get(k) for k in keys}

    def status(self) -> dict[str, Any]:
        cams = list(self.cams)
        first = cams[0] if cams else ""
        return {
            "enabled": self.enabled, "version": self.version, "paused": not self.enabled, "cameras": cams,
            "capabilities": {c: {"mode": self.mode(c)} for c in self.cfg.modes},
            "implemented": ["quality", "survey"], "survey": self.survey.status(first) if first else {}, "pending": self.store.count_decisions("pending"),
            "budgets": self.budgets(first) if first else {},
            "models": {"calls_today": self.store.calls_today(), "left": self._model_left()},
            "structural_in_auto": self.cfg.guardrails.structural_in_auto,
            "ha": {"mqtt": bool(self.cfg.notify.mqtt)},
            "reasons": {c.id: c.reason for c in self.cams.values() if c.reason},
        }

    def _model_left(self) -> int | None:
        if not self.decider.providers:
            return None
        return int(min(p.budget.left for p in self.decider.providers.values()))

    def _notify(self, row: dict[str, Any] | None, event: str) -> None:
        if self.notifier is None or row is None:
            return
        try:
            self.notifier.publish_decision(self.view(row), event)
            self._announce()
        except Exception:  # noqa: BLE001 - notification is best effort
            log.exception("assist: notification failed")

    def _announce(self) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.publish_status(self.status())
            pending = self.store.decisions(statuses=("pending",), limit=1)
            self.notifier.publish_pending(self.store.count_decisions("pending"), self.view(pending[0]) if pending else None)
        except Exception:  # noqa: BLE001
            log.exception("assist: status publish failed")

    def purge(self) -> None:
        self.store.purge()
        self.survey.purge()


def _metrics_from(raw: dict[str, Any]) -> Metrics:
    return Metrics(raw["sharp"], raw["clip_hi"], raw["clip_lo"], raw["luma"], raw["contrast"], raw["noise"], raw["glare"],
                   raw.get("glare_cx"), raw.get("glare_cy"), tuple(raw.get("glare_map") or ()))
