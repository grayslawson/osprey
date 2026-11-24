"""Stitching, the label grid, seam scores and map diffs. Pillow only.

The panorama is a plain degrees-by-degrees mosaic (equirectangular, no wrap-around). A
picture is first mapped from the lens's projection to angles, then feathered onto the canvas.
Nothing here talks to a camera.
"""

from __future__ import annotations

import io
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final

from PIL import Image, ImageChops

from third_party.raptor_vision import labels as rv_labels
from third_party.raptor_vision.geometry import rasterize
from third_party.raptor_vision.types import Region

from . import survey_plan as sp

# The vocabulary is raptor-vision's. The first twelve keep Osprey's old order because a stored labels.png holds indexes into
# this tuple; the new labels are appended, never inserted. ``building`` is not listed: it is the alias of ``wall_eaves``.
_LEGACY: Final = ("door", "gate", "driveway", "path", "street", "yard", "fence_line", "sky", "wall_eaves",
                  "foliage", "glare_source", "other")
LABELS: Final = (*_LEGACY, *(k for k in rv_labels.LABELS if k not in _LEGACY and k not in rv_labels.ALIASES.values()))
UNKNOWN: Final = rv_labels.UNKNOWN
COLORS: Final = rv_labels.COLORS
_STRIPS: Final = (40, 12)


def _remap(src: Image.Image, out_size: tuple[int, int], fn: Callable[[float, float], tuple[float, float]]) -> Image.Image:
    """Resample ``src`` into ``out_size``; ``fn`` maps normalised output (u, v in 0..1) to normalised source."""
    w, h = out_size
    sw, sh = src.size
    nx, ny = _STRIPS
    mesh = []
    for j in range(ny):
        for i in range(nx):
            x0, x1 = round(i * w / nx), round((i + 1) * w / nx)
            y0, y1 = round(j * h / ny), round((j + 1) * h / ny)
            quad: list[float] = []
            for (cx, cy) in ((x0, y0), (x0, y1), (x1, y1), (x1, y0)):
                u, v = fn(cx / w, cy / h)
                quad += [min(max(u, 0.0), 1.0) * sw, min(max(v, 0.0), 1.0) * sh]
            mesh.append(((x0, y0, x1, y1), tuple(quad)))
    return src.transform(out_size, Image.Transform.MESH, mesh, Image.Resampling.BILINEAR)


def to_angle_space(img: Image.Image, fov: tuple[float, float], ppd: float, projection: str = "rectilinear") -> Image.Image:
    """A picture from the camera as an image whose axes are degrees (ppd pixels per degree)."""
    size = (max(2, round(fov[0] * ppd)), max(2, round(fov[1] * ppd)))

    def fn(u: float, v: float) -> tuple[float, float]:
        x, y = sp.from_angles((u - 0.5) * fov[0], (0.5 - v) * fov[1], fov, projection)
        return (x + 1) / 2, (1 - y) / 2

    return _remap(img.convert("RGB"), size, fn)


def from_angle_space(img: Image.Image, fov: tuple[float, float], size: tuple[int, int],
                     projection: str = "rectilinear") -> Image.Image:
    """The inverse: what a lens would show of an angle-space image. Used by the simulator."""
    def fn(u: float, v: float) -> tuple[float, float]:
        dx, dy = sp.to_angles(2 * u - 1, 1 - 2 * v, fov, projection)
        return dx / fov[0] + 0.5, 0.5 - dy / fov[1]

    return _remap(img.convert("RGB"), size, fn)


@lru_cache(maxsize=32)
def _feather(size: tuple[int, int], frac: float) -> Image.Image:
    w, h = size
    rx, ry = max(1, round(w * frac)), max(1, round(h * frac))
    hx = Image.new("L", (w, 1))
    hx.putdata([min(255, round(255 * min(i + 1, w - i, rx) / rx)) for i in range(w)])
    vy = Image.new("L", (1, h))
    vy.putdata([min(255, round(255 * min(j + 1, h - j, ry) / ry)) for j in range(h)])
    return ImageChops.multiply(hx.resize((w, h)), vy.resize((w, h)))


def ncc(a: Image.Image, b: Image.Image, side: int = 48) -> float | None:
    """Correlation of two pictures' brightness, -1..1; None when either is too flat to tell."""
    ga = a.convert("L").resize((side, side))
    gb = b.convert("L").resize((side, side))
    xs, ys = list(ga.tobytes()), list(gb.tobytes())
    ma, mb = statistics.fmean(xs), statistics.fmean(ys)
    sa = math.sqrt(sum((x - ma) ** 2 for x in xs))
    sb = math.sqrt(sum((y - mb) ** 2 for y in ys))
    if sa < side * 2 or sb < side * 2:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(xs, ys, strict=True)) / (sa * sb)


@dataclass
class Placed:
    """One picture on the canvas."""

    cell: int
    az: float
    el: float
    zoom: float
    image: Image.Image         # in angle space
    box: tuple[int, int, int, int] = (0, 0, 0, 0)


class Mosaic:
    def __init__(self, geo: sp.Geometry, ppd: float, wide_zoom: float = 0.0, projection: str = "rectilinear") -> None:
        self.geo, self.ppd, self.projection = geo, ppd, projection
        self.az0, self.el0, self.az1, self.el1 = geo.extent(wide_zoom)
        self.size = (round((self.az1 - self.az0) * ppd), round((self.el1 - self.el0) * ppd))
        self.canvas = Image.new("RGB", self.size)
        self.cover = Image.new("L", self.size, 0)
        self.placed: list[Placed] = []

    def px(self, az: float, el: float) -> tuple[int, int]:
        return round((az - self.az0) * self.ppd), round((self.el1 - el) * self.ppd)

    def deg(self, x: float, y: float) -> tuple[float, float]:
        return self.az0 + x / self.ppd, self.el1 - y / self.ppd

    def add(self, jpeg: bytes, cell: int, pan: float, tilt: float, zoom: float) -> Placed:
        img = Image.open(io.BytesIO(jpeg))
        img.load()
        fov = self.geo.fov(zoom)
        tile = to_angle_space(img, fov, self.ppd, self.projection)
        az, el = self.geo.az(pan), self.geo.el(tilt)
        x, y = self.px(az - fov[0] / 2, el + fov[1] / 2)
        frac = 0.12 if zoom <= 0.2 else 0.05
        mask = _feather(tile.size, frac)
        self.canvas.paste(tile, (x, y), mask)
        self.cover.paste(255, (x, y, x + tile.width, y + tile.height), mask.point(lambda v: 255 if v > 8 else 0))
        placed = Placed(cell, az, el, zoom, tile, (x, y, x + tile.width, y + tile.height))
        self.placed.append(placed)
        return placed

    def seam(self, a: Placed, b: Placed) -> float | None:
        """Correlation of the overlap of two placed pictures; None when they do not overlap or are flat."""
        x0, y0 = max(a.box[0], b.box[0]), max(a.box[1], b.box[1])
        x1, y1 = min(a.box[2], b.box[2]), min(a.box[3], b.box[3])
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        ca = a.image.crop((x0 - a.box[0], y0 - a.box[1], x1 - a.box[0], y1 - a.box[1]))
        cb = b.image.crop((x0 - b.box[0], y0 - b.box[1], x1 - b.box[0], y1 - b.box[1]))
        return ncc(ca, cb)

    def coverage(self) -> float:
        hist = self.cover.resize((self.size[0] // 8 or 1, self.size[1] // 8 or 1)).histogram()
        return 1 - hist[0] / max(1, sum(hist))

    def crop(self, az: float, el: float, zoom: float, out_w: int = 480) -> Image.Image:
        """What the panorama shows at a view, as a rectilinear picture of the lens's shape."""
        fov = self.geo.fov(zoom)
        x, y = self.px(az - fov[0] / 2, el + fov[1] / 2)
        w, h = max(2, round(fov[0] * self.ppd)), max(2, round(fov[1] * self.ppd))
        region = Image.new("RGB", (w, h))
        region.paste(self.canvas, (-x, -y))
        return from_angle_space(region, fov, (out_w, max(2, round(out_w * fov[1] / fov[0]))), self.projection)

    def jpeg(self, quality: int = 85) -> bytes:
        buf = io.BytesIO()
        self.canvas.save(buf, "JPEG", quality=quality)
        return buf.getvalue()


# ----------------------------------------------------------------- label grid

LABEL_DEG: Final = 2.0   # one grid cell is 2 degrees square


@dataclass
class LabelGrid:
    """A label (an index into LABELS, 255 unknown) and a confidence for every 2-degree square."""

    az0: float
    el1: float
    w: int
    h: int
    label: bytearray = field(default_factory=bytearray)
    conf: bytearray = field(default_factory=bytearray)
    entry: bytearray = field(default_factory=bytearray)     # 0-255, how entry-like (model's `entry` answer)
    detail: bytearray = field(default_factory=bytearray)    # 1 where a closer picture set the label
    closer: bytearray = field(default_factory=bytearray)    # 0-255, how much a closer view was asked for

    @classmethod
    def blank(cls, az0: float, el0: float, az1: float, el1: float) -> LabelGrid:
        w, h = math.ceil((az1 - az0) / LABEL_DEG), math.ceil((el1 - el0) / LABEL_DEG)
        n = w * h
        return cls(az0, el1, w, h, bytearray([255]) * n, bytearray(n), bytearray(n), bytearray(n), bytearray(n))

    def cell_of(self, az: float, el: float) -> tuple[int, int] | None:
        x, y = int((az - self.az0) // LABEL_DEG), int((self.el1 - el) // LABEL_DEG)
        return (x, y) if 0 <= x < self.w and 0 <= y < self.h else None

    def center(self, x: int, y: int) -> tuple[float, float]:
        return self.az0 + (x + 0.5) * LABEL_DEG, self.el1 - (y + 0.5) * LABEL_DEG

    def paint(self, az0: float, el0: float, az1: float, el1: float, label: str, conf: float, entry: float,
              detail: bool, closer: float = 0.0) -> None:
        """Set a box. A closer picture always wins over a wide one; otherwise the more confident wins."""
        idx = LABELS.index(label) if label in LABELS else 255
        a, b = self.cell_of(max(az0, self.az0), min(el1, self.el1)), self.cell_of(az1 - 1e-6, el0 + 1e-6)
        if a is None or b is None:
            return
        c = max(1, min(255, round(conf * 255)))
        for y in range(a[1], b[1] + 1):
            for x in range(a[0], b[0] + 1):
                k = y * self.w + x
                if self.label[k] != 255 and not detail and (self.detail[k] or self.conf[k] > c):
                    continue
                self.label[k], self.conf[k], self.entry[k], self.detail[k] = idx, c, round(entry * 255), int(detail)
                self.closer[k] = round(closer * 255)

    def paint_regions(self, regions: list[Region], az: float, el: float, fov: tuple[float, float], projection: str,
                      detail: bool) -> None:
        """Paint labelled polygons (picture fractions) of one picture taken at (az, el) with field of view ``fov``.

        Each 2-degree square whose centre falls inside a polygon takes its label, with the same winner rules as ``paint``.
        ``entry`` and ``closer`` come from the region's ``extra``.
        """
        res = 128
        masks = [(r, rasterize(r.polygon, res, res)) for r in regions]
        a, b = self.cell_of(max(az - fov[0] / 2, self.az0), min(el + fov[1] / 2, self.el1)), self.cell_of(az + fov[0] / 2, el - fov[1] / 2)
        if a is None or b is None:
            return
        for y in range(a[1], b[1] + 1):
            for x in range(a[0], b[0] + 1):
                cx, cy = self.center(x, y)
                try:
                    px, py = sp.from_angles(cx - az, cy - el, fov, projection)
                except (ValueError, ZeroDivisionError):
                    continue
                if abs(px) > 1 or abs(py) > 1:
                    continue
                ix, iy = round((px + 1) / 2 * (res - 1)), round((1 - py) / 2 * (res - 1))
                for r, mask in masks:
                    if mask.getpixel((ix, iy)):
                        e, c = r.extra.get("entry", 0.0), r.extra.get("closer", 0.0)
                        self.paint(cx - 0.9, cy - 0.9, cx + 0.9, cy + 0.9, r.label, r.confidence,
                                   float(e) if isinstance(e, (int, float)) else 0.0, detail,
                                   float(c) if isinstance(c, (int, float)) else 0.0)

    def at(self, az: float, el: float) -> str:
        pos = self.cell_of(az, el)
        if pos is None:
            return UNKNOWN
        v = self.label[pos[1] * self.w + pos[0]]
        return LABELS[v] if v < len(LABELS) else UNKNOWN

    def png(self) -> bytes:
        img = Image.frombytes("L", (self.w, self.h), bytes(self.label))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    @classmethod
    def from_png(cls, data: bytes, az0: float, el1: float) -> LabelGrid:
        img = Image.open(io.BytesIO(data)).convert("L")
        n = img.width * img.height
        return cls(az0, el1, img.width, img.height, bytearray(img.tobytes()), bytearray([128]) * n, bytearray(n),
                   bytearray(n), bytearray(n))

    def overlay(self, mosaic_size: tuple[int, int], alpha: int = 120) -> bytes:
        """A translucent coloured picture of the labels, the size of the panorama."""
        rgba = bytearray()
        palette = [COLORS[name] for name in LABELS]
        for v in self.label:
            r, g, b = palette[v] if v < len(palette) else COLORS[UNKNOWN]
            rgba += bytes((r, g, b, alpha if v < len(palette) else 0))
        img = Image.frombytes("RGBA", (self.w, self.h), bytes(rgba)).resize(mosaic_size, Image.Resampling.NEAREST)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def _focus(self, cells: list[int]) -> list[float] | None:
        """The box around the squares the model flagged (entry-like or worth a closer look), at least 10 degrees."""
        if not cells:
            return None
        xs, ys = [k % self.w for k in cells], [k // self.w for k in cells]
        cx, cy = self.az0 + (min(xs) + max(xs) + 1) / 2 * LABEL_DEG, self.el1 - (min(ys) + max(ys) + 1) / 2 * LABEL_DEG
        hw = max((max(xs) - min(xs) + 1) * LABEL_DEG / 2, 5.0)
        hh = max((max(ys) - min(ys) + 1) * LABEL_DEG / 2, 5.0)
        return [cx - hw, cy - hh, cx + hw, cy + hh]

    def components(self, labels: tuple[str, ...]) -> list[dict[str, Any]]:
        """Connected regions (4-neighbour) of the wanted labels, with box, centre and size in degrees."""
        wanted = {LABELS.index(label) for label in labels}
        seen = bytearray(self.w * self.h)
        out: list[dict[str, Any]] = []
        for start in range(self.w * self.h):
            if seen[start] or self.label[start] not in wanted:
                continue
            idx = self.label[start]
            stack, cells = [start], []
            seen[start] = 1
            while stack:
                k = stack.pop()
                cells.append(k)
                x, y = k % self.w, k // self.w
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    n = ny * self.w + nx
                    if 0 <= nx < self.w and 0 <= ny < self.h and not seen[n] and self.label[n] == idx:
                        seen[n] = 1
                        stack.append(n)
            xs, ys = [k % self.w for k in cells], [k // self.w for k in cells]
            az0, az1 = self.az0 + min(xs) * LABEL_DEG, self.az0 + (max(xs) + 1) * LABEL_DEG
            el1, el0 = self.el1 - min(ys) * LABEL_DEG, self.el1 - (max(ys) + 1) * LABEL_DEG
            out.append({"label": LABELS[idx], "cells": len(cells), "area": len(cells) * LABEL_DEG ** 2,
                        "bbox": [az0, el0, az1, el1],
                        "center": [self.az0 + (statistics.fmean(xs) + 0.5) * LABEL_DEG,
                                   self.el1 - (statistics.fmean(ys) + 0.5) * LABEL_DEG],
                        "conf": statistics.fmean(self.conf[k] for k in cells) / 255,
                        "entry": (max if LABELS[idx] in ("door", "gate") else statistics.fmean)(
                            self.entry[k] for k in cells) / 255,
                        "closer": max(self.closer[k] for k in cells) / 255,
                        "focus": self._focus([k for k in cells if self.entry[k] >= 128 or self.closer[k] >= 128]),
                        "detail": any(self.detail[k] for k in cells)})
        return out


# ------------------------------------------------------------------------ diff

BLOCK_DEG: Final = 10.0


def diff_maps(old: Image.Image, new: Image.Image, az0: float, el1: float, ppd: float,
              threshold: float = 0.45) -> list[dict[str, Any]]:
    """Blocks of 10 degrees that look different between two panoramas of the same kind and geometry."""
    if old.size != new.size:
        new = new.resize(old.size)
    bs = round(BLOCK_DEG * ppd)
    out: list[dict[str, Any]] = []
    for y in range(0, old.height - bs + 1, bs):
        for x in range(0, old.width - bs + 1, bs):
            a, b = old.crop((x, y, x + bs, y + bs)), new.crop((x, y, x + bs, y + bs))
            ga, gb = a.convert("L"), b.convert("L")
            if max(ga.tobytes()) < 4 or max(gb.tobytes()) < 4:   # an uncovered block: not a change, a gap
                continue
            c = ncc(a, b)
            ma, mb = statistics.fmean(ga.resize((16, 16)).tobytes()), statistics.fmean(gb.resize((16, 16)).tobytes())
            mad = statistics.fmean(abs((p - ma) - (q - mb)) for p, q in
                                   zip(ga.resize((16, 16)).tobytes(), gb.resize((16, 16)).tobytes(), strict=True))
            score = (1 - c) / 2 + min(mad / 40, 1) * 0.5 if c is not None else min(mad / 25, 1)
            if score >= threshold:
                out.append({"x": x, "y": y, "az": az0 + (x + bs / 2) / ppd, "el": el1 - (y + bs / 2) / ppd,
                            "score": round(score, 3)})
    return out
