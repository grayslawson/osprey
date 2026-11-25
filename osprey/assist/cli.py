"""``osprey assist``: run the helper, score models on labelled frames, or forget everything."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import evalcmd
from .client import OspreyClient
from .decide import Decider
from .engine import Engine
from .notify import MqttAdapter, Notifier, PahoAdapter
from .providers import build_provider
from .server import AssistServer
from .signals import Signals
from .store import Store

log = logging.getLogger("osprey.assist")


def build(cfg: config_mod.AssistConfig, *, adapter: MqttAdapter | None = None) -> tuple[Engine, AssistServer, Notifier | None]:
    """Wire the pieces together. Nothing is started."""
    store = Store(cfg.state_dir)
    providers = {name: build_provider(name, p) for name, p in cfg.providers.items()}
    decider = Decider(cfg.tasks, providers, store.log_call, cfg.privacy.redact_names)
    cameras = {cid: OspreyClient(cfg.osprey.url, cfg.osprey.token_file, cid) for cid in cfg.osprey.cameras}
    engine = Engine(cfg, store, cameras, decider, signals=Signals(cfg.tasks, providers, store.log_call))  # type: ignore[arg-type]  # OspreyClient implements Camera
    notifier: Notifier | None = None
    if cfg.notify.mqtt or cfg.notify.ntfy.url:
        from .notify import Callbacks

        def decide(decision_id: int, verdict: str, reason: str, principal: str) -> None:
            if verdict == "approve":
                engine.approve(decision_id, principal)
            else:
                engine.reject(decision_id, principal, reason)

        def newest() -> dict[str, Any] | None:
            rows = store.decisions(statuses=("pending",), limit=1)
            return engine.view(rows[0]) if rows else None

        def check_now() -> None:
            engine.check(next(iter(engine.cams)), "manual")

        def start_survey() -> None:
            engine.survey.start(next(iter(engine.cams)), "manual")

        mqtt_adapter = adapter
        if mqtt_adapter is None and cfg.notify.mqtt:
            try:
                mqtt_adapter = PahoAdapter(cfg.notify.broker)
            except RuntimeError as exc:
                log.warning("MQTT is off: %s", exc)
        notifier = Notifier(cfg.notify, mqtt_adapter,
                            Callbacks(decide, engine.set_enabled, engine.set_mode, check_now, newest, start_survey,
                                      engine.survey.cancel), store.frame)
        engine.notifier = notifier
    server = AssistServer((cfg.listen.bind, cfg.listen.port), engine, cfg.osprey.token_file)
    return engine, server, notifier


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": args.mode, "provider": args.provider, "model": args.model}
    if args.send_frames:
        out["send_frames"] = True
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="osprey assist", description="Osprey Assist: the optional image-quality helper.")
    parser.add_argument("--config", help="path to osprey-assist.json (default: $OSPREY_ASSIST_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the helper")
    ev = sub.add_parser("eval", help="score a task on labelled frames")
    ev.add_argument("--cases", required=True, help="folder with cases.json and the frames")
    ev.add_argument("--task", default="quality.diagnose", choices=config_mod.TASKS)
    ev.add_argument("--mode", choices=("rules", "model", "both"))
    ev.add_argument("--provider")
    ev.add_argument("--model")
    ev.add_argument("--send-frames", action="store_true", help="send frames (the provider must allow them too)")
    ev.add_argument("--out", help="write the full result as JSON here")
    rp = sub.add_parser("replay", help="score a task on frames you approved or rejected")
    rp.add_argument("--task", default="quality.diagnose", choices=config_mod.TASKS)
    rp.add_argument("--mode", choices=("rules", "model", "both"))
    rp.add_argument("--provider")
    rp.add_argument("--model")
    rp.add_argument("--send-frames", action="store_true")
    rp.add_argument("--out")
    sv = sub.add_parser("survey", help="sweep the camera's whole range and map what it sees")
    sv.add_argument("--camera", help="camera id (default: the first one Assist watches)")
    what = sv.add_mutually_exclusive_group()
    what.add_argument("--plan", action="store_true", help="print the grid and the time it should take, and move nothing")
    what.add_argument("--status", action="store_true", help="show the current or last survey")
    what.add_argument("--cancel", action="store_true", help="stop a running survey")
    what.add_argument("--resume", action="store_true", help="carry on an interrupted survey")
    what.add_argument("--apply", action="store_true", help="apply every waiting proposal")
    what.add_argument("--revert", action="store_true", help="undo the last apply")
    sv.add_argument("--wait", action="store_true", help="follow the survey until it ends")
    pg = sub.add_parser("purge", help="forget every frame and everything learned")
    pg.add_argument("--yes", action="store_true", help="really do it")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        cfg = config_mod.load(args.config)
    except ValueError as exc:
        print(f"osprey assist: {exc}", file=sys.stderr)
        return 2
    if args.command == "survey":
        return _survey(cfg, args)
    if args.command == "purge":
        if not args.yes:
            print("This deletes every stored frame and all learned baselines. Run again with --yes.", file=sys.stderr)
            return 2
        Store(cfg.state_dir).purge()
        print("Assist's stored data is gone.")
        return 0
    if args.command in ("eval", "replay"):
        try:
            if args.command == "replay":
                cases = Path(tempfile.mkdtemp(prefix="assist-replay-"))
                count = evalcmd.cases_from_store(Store(cfg.state_dir), cases)
                if not count:
                    print("No approved or rejected decisions with frames to replay yet.", file=sys.stderr)
                    return 1
            else:
                cases = Path(args.cases)
            result = evalcmd.run_eval(cfg, cases, args.task, _overrides(args))
        except (OSError, ValueError, KeyError) as exc:
            print(f"osprey assist: {exc}", file=sys.stderr)
            return 2
        summary = {k: v for k, v in result.items() if k != "detail"}
        print(json.dumps(summary, indent=1))
        if args.out:
            Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
        return 0
    return _run(cfg)


def _api(cfg: config_mod.AssistConfig, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call the running Assist on its own address with the shared token."""
    token = Path(cfg.osprey.token_file).read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        f"http://{cfg.listen.bind}:{cfg.listen.port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Osprey-Principal": "cli"})
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:  # noqa: S310 - the operator's own address
            return dict(json.loads(reply.read()))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read())["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = exc.reason
        raise RuntimeError(str(message)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Assist is not answering on {cfg.listen.bind}:{cfg.listen.port} ({type(exc).__name__}). "
                           "Is `osprey assist run` going?") from None


def _line(status: dict[str, Any]) -> str:
    run = status.get("run") or {}
    if not run:
        return "No survey has run yet."
    done = f"{run.get('done', 0)} of {run.get('total', 0)}"
    why = f" ({run['reason']})" if run.get("reason") else ""
    return f"Survey {run['id']}, {run['kind']}: {run['status']}, {run.get('phase', '')}, {done}{why}"


def _survey(cfg: config_mod.AssistConfig, args: argparse.Namespace) -> int:
    camera = args.camera or cfg.osprey.cameras[0]
    q = f"?camera={urllib.parse.quote(camera)}"
    try:
        if args.plan:
            from . import survey_plan as sp
            client = OspreyClient(cfg.osprey.url, cfg.osprey.token_file, camera)
            geo = sp.Geometry.from_state((client.state().get("ptz") or {}).get("fov"))
            if geo is None and cfg.survey.fov:
                geo = sp.Geometry.from_config(cfg.survey.fov)
            if geo is None:
                print("This camera has no field-of-view calibration. Add survey.fov to osprey-assist.json.", file=sys.stderr)
                return 2
            cells = sp.plan(geo, cfg.survey.overlap, cfg.survey.wide_zoom)
            cols, rows = sp.grid_size(geo, cfg.survey.overlap, cfg.survey.wide_zoom)
            est = sp.estimate_sec(geo, cells, cfg.survey.max_zoom_ins,
                                  sp.Timing(settle_sec=cfg.survey.settle_sec, zoom_settle_sec=cfg.survey.zoom_settle_sec,
                                            deg_per_sec=sp.BASE_DEG_PER_SEC * cfg.survey.speed))
            print(f"{cols} columns by {rows} rows, {len(cells)} pictures, up to {cfg.survey.max_zoom_ins} closer looks.")
            print(f"About {est['total_sec'] / 60:.1f} minutes of camera time ({est['sweep_sec']:.0f} s sweep, "
                  f"{est['zoom_sec']:.0f} s zoom-ins). An estimate from the geometry, not a measurement.")
            return 0
        if args.cancel:
            _api(cfg, "POST", "/survey/cancel", {"camera": camera})
            print("Asked the survey to stop.")
            return 0
        if args.apply or args.revert:
            out = _api(cfg, "POST", "/survey/revert" if args.revert else "/survey/apply", {"camera": camera})
            print(json.dumps(out, indent=1))
            return 0
        if args.resume:
            _api(cfg, "POST", "/survey/resume", {"camera": camera})
        elif not args.status:
            _api(cfg, "POST", "/survey/start", {"camera": camera})
            print("Survey started.")
        status = _api(cfg, "GET", "/survey/status" + q)
        print(_line(status))
        while args.wait and status.get("busy"):
            time.sleep(3)
            status = _api(cfg, "GET", "/survey/status" + q)
            print(_line(status), flush=True)
        run = status.get("run") or {}
        if args.wait and run.get("status") in ("ready", "partial"):
            print(f"Done. {status.get('pending', 0)} proposal(s) wait in the console's Survey tab.")
        return 0 if not args.wait or run.get("status") in ("ready", "partial") else 1
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"osprey assist: {exc}", file=sys.stderr)
        return 2


def _run(cfg: config_mod.AssistConfig) -> int:
    if not cfg.enabled:
        print("Assist is off. Set \"enabled\": true in osprey-assist.json to run it.", file=sys.stderr)
        return 2
    engine, server, notifier = build(cfg)
    try:
        from version import get_version  # osprey/version.py, a plain module beside this package
        engine.version = get_version()
    except ImportError:
        engine.version = ""
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    if notifier is not None:
        notifier.start()
    server.start()
    engine.start()
    log.info("Assist is listening on %s:%d", cfg.listen.bind, cfg.listen.port)
    done.wait()
    engine.stop()
    server.shutdown()
    if notifier is not None:
        notifier.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
