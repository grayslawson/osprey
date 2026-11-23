"""Process entry point: version flags and the signal shutdown path."""

from __future__ import annotations

import signal
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import logbuffer
import logfile
import main
from version import get_version

ROOT = Path(__file__).resolve().parents[2]


def test_version_comes_from_the_version_file() -> None:
    assert get_version() == (ROOT / "VERSION").read_text().strip()


def test_version_flag_and_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == get_version()
    with pytest.raises(SystemExit) as exc:
        main.parse_args(["--version"])
    assert exc.value.code == 0
    assert get_version() in capsys.readouterr().out


class FakeController:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def stop(self) -> None:
        self.order.append("controller.stop")


class FakeStack:
    def __init__(self, handlers: dict[int, Any], order: list[str]) -> None:
        self.handlers = handlers
        self.order = order
        self.controller = FakeController(order)
        self.services = SimpleNamespace(setup=None)

    def run(self) -> None:
        # A SIGTERM arrives while the control loop is serving.
        self.handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.order.append("run returned")

    def stop(self) -> None:
        self.order.append("stack.stop")


def test_signal_handler_only_sets_the_stop_flag_and_main_tears_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handlers: dict[int, Any] = {}
    order: list[str] = []
    options = SimpleNamespace(
        cert=tmp_path / "cert.pem", control_port=1, ingest_port=2, snapshot_port=3, rtsp_port=4, onvif_port=5
    )
    monkeypatch.setattr(signal, "signal", lambda num, handler: handlers.__setitem__(num, handler))
    monkeypatch.setattr(main, "resolve_options", lambda args: options)
    monkeypatch.setattr(main, "build", lambda opts: FakeStack(handlers, order))
    monkeypatch.setattr(logbuffer, "install", lambda: None)
    monkeypatch.setattr(logfile, "install", lambda *a, **k: None)

    assert main.main([]) == 0

    # The handler stopped nothing but the controller loop flag; the full
    # teardown ran afterwards, once, from main().
    assert order == ["controller.stop", "run returned", "stack.stop"]
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}


def test_a_saved_setup_restarts_the_process_onto_the_new_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wizard's save stops the loop and re-executes; no manual restart."""
    order: list[str] = []
    setup = SimpleNamespace(on_complete=None)
    options = SimpleNamespace(
        cert=tmp_path / "cert.pem", control_port=1, ingest_port=2, snapshot_port=3, rtsp_port=4, onvif_port=5
    )

    class SavingStack(FakeStack):
        def run(self) -> None:
            setup.on_complete()  # the browser saved the wizard
            deadline = time.time() + 5
            while "controller.stop" not in self.order and time.time() < deadline:
                time.sleep(0.02)
            self.order.append("run returned")

    stack = SavingStack({}, order)
    stack.services = SimpleNamespace(setup=setup)
    monkeypatch.setattr(signal, "signal", lambda num, handler: None)
    monkeypatch.setattr(main, "resolve_options", lambda args: options)
    monkeypatch.setattr(main, "build", lambda opts: stack)
    monkeypatch.setattr(main, "reexec", lambda: order.append("reexec"))
    monkeypatch.setattr(logbuffer, "install", lambda: None)
    monkeypatch.setattr(logfile, "install", lambda *a, **k: None)

    assert main.main([]) == 0
    assert order == ["controller.stop", "run returned", "stack.stop", "reexec"]
