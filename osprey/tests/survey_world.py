"""A camera simulator for Survey: a panoramic world of known places that the real web API renders from pan, tilt and zoom.

The world is an image in degrees. Each place has a flat colour, so a stub model can name what it is shown.
``PanoWorld`` is the ``assist_world.World`` (real web API, token, arbitration, patrol) with its snapshot drawn
from that world, a motor that can miss its target, and scripted moments when Frigate or a person takes over.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
import io
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

import assist_scenes as scenes
from assist import survey_map as sm
from assist import survey_plan as sp
from assist_world import Clock, World
from model import Position

PPD = 4
GEO = sp.Geometry(350, 100, (99.7, 51.9), (45.5, 25.4))
EXTENT = GEO.extent(0.0)
# (az0, el0, az1, el1) of every place, drawn in this order. Chroma, not brightness, tells them apart.
PLACES: list[tuple[str, tuple[float, float, float, float]]] = [
    ("yard", (-300, -90, 300, 90)),
    ("sky", (-300, 28, 300, 90)),
    ("foliage", (-300, -20, -130, 28)),
    ("foliage", (150, -10, 300, 28)),
    ("wall_eaves", (-125, -14, -45, 28)),
    ("wall_eaves", (-40, 6, 45, 28)),
    ("fence_line", (-40, -8, 45, 6)),
    ("gate", (12, -8, 24, 6)),
    ("door", (-95, -14, -82, 4)),
    ("driveway", (45, -60, 120, -14)),
    ("path", (-100, -60, -70, -14)),
    ("street", (125, -60, 215, -20)),
]
COLORS = {"yard": (130, 190, 90), "sky": (100, 170, 235), "foliage": (40, 120, 50), "wall_eaves": (170, 165, 150),
          "fence_line": (170, 100, 170), "gate": (230, 140, 30), "door": (140, 80, 40), "driveway": (100, 115, 140),
          "path": (200, 170, 110), "street": (90, 90, 60), "other": (200, 40, 40), "glare_source": (255, 255, 255)}


@functools.lru_cache(maxsize=8)
def world_image(night: bool, car: bool, glare: tuple[float, float, float] | None) -> Image.Image:
    az0, el0, az1, el1 = EXTENT
    w, h = round((az1 - az0) * PPD), round((el1 - el0) * PPD)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)

    def box(b: tuple[float, float, float, float]) -> list[float]:
        return [(b[0] - az0) * PPD, (el1 - b[3]) * PPD, (b[2] - az0) * PPD, (el1 - b[1]) * PPD]

    for name, b in PLACES:
        d.rectangle(box(b), fill=COLORS[name])
    if car:
        d.rectangle(box((60, -40, 82, -26)), fill=COLORS["other"])
    rng = random.Random(7)
    for _ in range(w * h // 400):    # texture, so the stitcher has something to match
        x, y = rng.randrange(w), rng.randrange(h)
        red, green, blue = (int(v) for v in img.getpixel((x, y)))  # type: ignore[union-attr]
        s = rng.randint(-14, 14)
        d.rectangle([x, y, x + 2, y + 2], fill=(max(0, red + s), max(0, green + s), max(0, blue + s)))
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    if night:
        img = img.point(lambda v: int(v * 0.42))
        if glare:
            gaz, gel, gr = glare
            cx, cy = (gaz - az0) * PPD, (el1 - gel) * PPD
            mask = Image.new("L", img.size, 0)
            ImageDraw.Draw(mask).ellipse([cx - gr * PPD, cy - gr * PPD, cx + gr * PPD, cy + gr * PPD], fill=255)
            img = Image.composite(Image.new("RGB", img.size, (255, 255, 255)), img, mask.filter(ImageFilter.GaussianBlur(3)))
    return img


class PanoWorld(World):
    def __init__(self, tmp: Path, clock: Clock, *, night: bool = False, car: bool = False, glare: tuple[float, float, float] | None = (44.0, 4.0, 7.0),
                 presets: bool = False, size: tuple[int, int] = (640, 360)) -> None:
        super().__init__(tmp, clock, night=night, glare=None, presets=presets)
        self.car, self.glare_on, self.size = car, glare, size
        self.camera.model = "UVC G5 PTZ"
        self.miss: list[float] = []              # extra pan error (normalised) for the next moves, popped in order
        self.frames = 0
        self.busy_windows: list[tuple[float, float, dict[str, object]]] = []   # (from, until, signals) on the clock
        self.fail_snapshots = 0
        self.black_snapshots = 0
        self.flip_scene_at: float | None = None
        self.move_times: list[float] = []
        self.on_move: Callable[[int], None] | None = None
        self._patch()

    def _patch(self) -> None:
        backend = self.service.backend
        backend.signals = self._signals
        backend.remove_preset = self._remove_preset
        backend.snapshot = self.render

    def _signals(self) -> dict[str, object]:
        now = self.clock()
        for start, until, sig in self.busy_windows:
            if start <= now < until:
                return dict(sig)
        return {}

    def _remove_preset(self, token: int) -> bool:
        return self.camera.presets.pop(token, None) is not None

    # -- the motor
    def _move(self, position: Position, speed: int) -> bool:
        self.move_times.append(self.clock())
        if self.on_move is not None:
            self.on_move(len(self.move_times))
        if self.miss:
            from tests_support import PAN_RANGE
            err = self.miss.pop(0)
            span = PAN_RANGE.maximum - PAN_RANGE.minimum
            position = Position(pan=PAN_RANGE.clamp(position.pan + round(err * span / 2)), tilt=position.tilt,
                                zoom=position.zoom, focus=position.focus)
        return super()._move(position, speed)

    # -- optics
    def view(self) -> tuple[float, float, float]:
        raw = self.camera.motion.position
        pan = self.camera.pan_range.to_normalised(raw.pan)
        tilt = -self.camera.tilt_range.to_normalised(raw.tilt)
        zoom = (self.camera.zoom_range.to_normalised(raw.zoom) + 1) / 2
        return pan, tilt, zoom

    def render(self) -> bytes:
        self.frames += 1
        if self.fail_snapshots > 0:
            self.fail_snapshots -= 1
            raise RuntimeError("no snapshot")
        if self.flip_scene_at is not None and self.clock() >= self.flip_scene_at:
            self.camera.scene = "DAY" if self.camera.scene == "NIGHT" else "NIGHT"
            self.flip_scene_at = None
        night = self.camera.scene == "NIGHT"
        if self.black_snapshots > 0:
            self.black_snapshots -= 1
            return scenes.jpeg(Image.new("RGB", self.size, (0, 0, 0)))
        pan, tilt, zoom = self.view()
        fov = GEO.fov(zoom)
        az, el = GEO.az(pan), GEO.el(tilt)
        world = world_image(night, self.car, self.glare_on)
        az0, _, _, el1 = EXTENT
        x, y = round((az - fov[0] / 2 - az0) * PPD), round((el1 - el - fov[1] / 2) * PPD)
        region = Image.new("RGB", (round(fov[0] * PPD), round(fov[1] * PPD)))
        region.paste(world, (-x, -y))
        frame = sm.from_angle_space(region, fov, self.size)
        return scenes.jpeg(frame)


class StubFrigate:
    """/api/events with scripted events: ``events`` is a list of dicts served as-is."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.requests: list[str] = []
        self._srv: ThreadingHTTPServer | None = None
        self.url = ""

    def __enter__(self) -> StubFrigate:
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                stub.requests.append(self.path)
                body = json.dumps(stub.events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._srv.serve_forever, args=(0.02,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        return self

    def __exit__(self, *exc: object) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()


def label_of_image(jpeg_b64_uri: str) -> tuple[str, float, float, float]:
    """What a good-enough model says of a picture: its main place by chroma, how pure it is, how entry-like, how mixed."""
    import base64
    data = base64.b64decode(jpeg_b64_uri.split(",", 1)[1])
    img = Image.open(io.BytesIO(data)).convert("RGB").resize((32, 18))
    chroma = {k: _chroma(v) for k, v in COLORS.items() if k not in ("glare_source",)}
    counts: dict[str, int] = {}
    for px in img.get_flattened_data():
        c = _chroma(tuple(int(v) for v in px))  # type: ignore[union-attr]
        best = min(chroma, key=lambda k: sum((a - b) ** 2 for a, b in zip(chroma[k], c, strict=True)))
        counts[best] = counts.get(best, 0) + 1
    total = 32 * 18
    main = max(counts, key=lambda k: counts[k])
    entry = max(counts.get("door", 0), counts.get("gate", 0)) / total
    return main, counts[main] / total, min(1.0, entry * 8), 0.9 if counts[main] / total < 0.8 else 0.1


def _chroma(px: tuple[int, ...]) -> tuple[float, float, float]:
    s = max(1, sum(px[:3]))
    return px[0] / s, px[1] / s, px[2] / s


def decisions_reply(body: dict[str, Any]) -> dict[str, Any]:
    """The stub /decisions endpoint for survey.label: reads the picture it was sent."""
    from stub_models import decisions_body
    image = next((s["image_url"]["url"] for s in body["state"] if isinstance(s, dict)), None)
    if image is None or "kind" not in body.get("questions", {}):
        return decisions_body({"change": {"type": "choice", "choice": "moved_object", "probabilities": {"moved_object": 0.9}}})
    label, purity, entry, closer = label_of_image(image)
    return decisions_body({
        "kind": {"type": "choice", "choice": label, "probabilities": {label: round(max(0.3, purity), 3)}},
        "entry": {"type": "noul", "noul": entry}, "closer": {"type": "noul", "noul": closer}})
