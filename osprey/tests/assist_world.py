"""A simulated camera and Osprey for Assist's closed-loop tests.

A real ``OnvifServer`` (real web API, token, arbitration and patrol hold) sits on a backend whose
snapshot is rendered from the camera's position and the ISP values it was sent. So Assist's
decisions change what Assist sees next, which is the loop being tested.
"""

from __future__ import annotations

import functools
import json
import time
from pathlib import Path
from typing import Any

from PIL import Image

import assist_scenes as scenes
import assist_scope
import onvif
from assist.client import OspreyClient
from model import Camera, Position, Preset
from sentry_manager import SentryManager
from sentry_runtime import MovementGate, SentryConfig
from tests_support import PAN_RANGE, a_camera
from test_onvif import Recorder

TOKEN = "w" * 40


@functools.lru_cache(maxsize=8)
def _base(night: bool, index: int) -> Image.Image:
    return scenes.scene(night=night, seed=index)
ISP_ALLOWED = set(assist_scope.ISP_ALLOW)


class Clock:
    """Simulated time. ``sleep`` advances it, so waits cost nothing."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class World:
    def __init__(self, tmp: Path, clock: Clock, *, night: bool = True, glare: tuple[float, float, float] | None = (0.93, 0.4, 0.26),
                 exposure: float = 1.0, presets: bool = True) -> None:
        self.clock = clock
        self.camera: Camera = a_camera()
        self.night = night
        self.glare = glare               # blob centre x, y and radius in the preset's own view
        self.exposure = exposure
        self.defocus = False
        self.perverse = False            # a world where lowering aeTargetPercent brightens the picture
        self.isp: dict[str, float] = {"aeTargetPercent": 50}
        self.isp_log: list[dict[str, Any]] = []
        self.moves: list[tuple[float, float, float]] = []
        self.move_speeds: list[int] = []
        self.base: dict[int, Position] = {}
        self.camera.scene = "NIGHT" if night else "DAY"
        self.signals: dict[str, object] = {}
        for name, position in (("BackPorch", Position(pan=12000, tilt=12000, zoom=300, focus=50)),
                               ("Gate", Position(pan=24000, tilt=13000, zoom=200, focus=50))) if presets else ():
            self.base[self.add_preset(name, position)] = position  # the two real views; saved views reuse them
        if presets:
            self.camera.motion.update(self.camera.presets[1].position, activity=0)
        self.recorder = Recorder(self.camera)
        backend = self.recorder.backend()
        backend.snapshot = self.render
        backend.move_absolute = self._move
        backend.goto_preset = self._goto
        backend.set_preset = self._set_preset
        backend.change_isp = self._isp
        backend.signals = lambda: dict(self.signals)
        self.manager = SentryManager(
            goto_preset=lambda cam, name, speed: self._goto_name(name), tracking_active=lambda c, h: False,
            camera_idle=lambda c: True, gate=MovementGate(), preset_names=lambda c: [p.name for p in self.camera.presets.values()])
        if presets:
            self.manager.configure(self.camera.mac, SentryConfig.from_mapping({
                "enabled": True, "interval_sec": 3600, "points": [{"name": "BackPorch"}, {"name": "Gate"}]}))
        backend.sentry = lambda: self.manager
        self.service = onvif.Services(backend, host="192.0.2.10", port=8000, auth=onvif.AdminAuth(allow_anonymous=True))
        self.server = onvif.OnvifServer(self.service, port=0)
        self.server.web.assist = assist_scope.AssistLink(TOKEN, frozenset({"quality", "presets", "patrol"}), "http://127.0.0.1:1")
        self.server.start()
        self.token_file = tmp / "assist-token"
        self.token_file.write_text(TOKEN, encoding="utf-8")
        self.client = OspreyClient(f"http://127.0.0.1:{self.server.port}", str(self.token_file))

    # -- camera behaviour
    def add_preset(self, name: str, position: Position) -> int:
        index = max(self.camera.presets, default=0) + 1
        self.camera.presets[index] = Preset(index, name, position)
        return index

    def _move(self, position: Position, speed: int) -> bool:
        if position.zoom != self.camera.motion.position.zoom:
            self.defocus = False  # a zoom nudge re-triggers autofocus
        self.camera.motion.update(position, activity=0)
        self.moves.append((position.pan, position.tilt, position.zoom))
        self.move_speeds.append(speed)
        return True

    def _goto(self, index: int, speed: int) -> bool:
        self.camera.motion.update(self.camera.presets[index].position, activity=0)
        return True

    def _goto_name(self, name: str) -> bool:
        for index, preset in self.camera.presets.items():
            if preset.name.casefold() == name.casefold():
                return self._goto(index, 1)
        return False

    def _set_preset(self, name: str, index: int | None) -> int | None:
        return self.add_preset(name, self.camera.motion.position)

    def _isp(self, settings: dict[str, Any]) -> bool:
        self.isp.update(settings)
        self.isp_log.append(dict(settings))
        return True

    # -- optics
    def _nearest(self) -> tuple[int, Position]:
        here = self.camera.motion.position
        index = min(self.base, key=lambda i: abs(self.base[i].pan - here.pan) + abs(self.base[i].tilt - here.tilt))
        return index, self.base[index]

    def render(self) -> bytes:
        index, ref = self._nearest()
        here = self.camera.motion.position
        span_pan = PAN_RANGE.maximum - PAN_RANGE.minimum
        dx = (here.pan - ref.pan) / span_pan * 6.0  # 1% of the pan span moves the view 6% of the frame
        dy = (here.tilt - ref.tilt) / span_pan * 6.0
        img = _base(self.night, index)
        img = scenes.shifted(img, -dx, -dy)
        if self.glare is not None:
            cx, cy, r = self.glare
            img = scenes.add_glare(img, cx - dx, cy - dy, r)
        ae = float(self.isp.get("aeTargetPercent", 50))
        gain = self.exposure * ((100 - ae) / 50 if self.perverse else ae / 50)
        img = scenes.expose(img, gain)
        if self.defocus:
            img = scenes.blur(img, 3.0)
        return scenes.jpeg(img)

    def close(self) -> None:
        self.manager.stop()
        self.server.stop()

    def preset_names(self) -> list[str]:
        return [p.name for p in self.camera.presets.values()]


def write_cases(folder: Path, labels: dict[str, tuple[str, Image.Image]]) -> None:
    """A tiny labelled set for ``osprey assist eval``."""
    folder.mkdir(parents=True, exist_ok=True)
    cases = []
    for name, (label, image) in labels.items():
        (folder / f"{name}.jpg").write_bytes(scenes.jpeg(image))
        cases.append({"id": name, "file": f"{name}.jpg", "scene": "NIGHT", "preset": "p", "label": label})
    (folder / "cases.json").write_text(json.dumps(cases), encoding="utf-8")


__all__ = ["Clock", "ISP_ALLOWED", "TOKEN", "World", "time", "write_cases"]
