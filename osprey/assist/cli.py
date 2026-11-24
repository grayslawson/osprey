"""``osprey assist``: run the helper, score models on labelled frames, or forget everything."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import tempfile
import threading
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
from .store import Store

log = logging.getLogger("osprey.assist")


def build(cfg: config_mod.AssistConfig, *, adapter: MqttAdapter | None = None) -> tuple[Engine, AssistServer, Notifier | None]:
    """Wire the pieces together. Nothing is started."""
    store = Store(cfg.state_dir)
    providers = {name: build_provider(name, p) for name, p in cfg.providers.items()}
    decider = Decider(cfg.tasks, providers, store.log_call, cfg.privacy.redact_names)
    cameras = {cid: OspreyClient(cfg.osprey.url, cfg.osprey.token_file, cid) for cid in cfg.osprey.cameras}
    engine = Engine(cfg, store, cameras, decider)  # type: ignore[arg-type]  # OspreyClient implements Camera
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

        mqtt_adapter = adapter
        if mqtt_adapter is None and cfg.notify.mqtt:
            try:
                mqtt_adapter = PahoAdapter(cfg.notify.broker)
            except RuntimeError as exc:
                log.warning("MQTT is off: %s", exc)
        notifier = Notifier(cfg.notify, mqtt_adapter,
                            Callbacks(decide, engine.set_enabled, engine.set_mode, check_now, newest), store.frame)
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
    pg = sub.add_parser("purge", help="forget every frame and everything learned")
    pg.add_argument("--yes", action="store_true", help="really do it")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        cfg = config_mod.load(args.config)
    except ValueError as exc:
        print(f"osprey assist: {exc}", file=sys.stderr)
        return 2
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
