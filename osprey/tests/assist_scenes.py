"""Deterministic synthetic camera scenes (Pillow only) for Assist tests and simulators."""

from __future__ import annotations

import io
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter


def _noise_layer(size: tuple[int, int], rng: random.Random, sigma: float) -> Image.Image:
    # shortcut: gaussian per-pixel via bytes from random.gauss is slow; uniform bytes scaled is enough
    raw = Image.frombytes("L", size, rng.randbytes(size[0] * size[1]))
    return raw.point(lambda v: int(128 + (v - 128) * sigma / 74))


def scene(kind: str = "yard", *, night: bool = False, size: tuple[int, int] = (960, 540), seed: int = 1) -> Image.Image:
    w, h = size
    rng = random.Random(seed)
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    horizon = int(h * 0.45)
    for y in range(horizon):  # gradient sky
        t = y / horizon
        d.line([(0, y), (w, y)], fill=(int(90 + 80 * t), int(130 + 70 * t), int(200 + 30 * t)))
    d.rectangle([0, horizon, w, h], fill=(110, 100, 85))
    # building with brick rows
    bx0, bx1, by0 = int(w * 0.12), int(w * 0.62), int(h * 0.12)
    d.rectangle([bx0, by0, bx1, horizon + 10], fill=(150, 70, 55))
    bh, bw = max(6, h // 54), max(14, w // 40)
    for row, y in enumerate(range(by0, horizon + 10, bh)):
        off = (row % 2) * bw // 2
        for x in range(bx0 - off, bx1, bw):
            shade = rng.randint(-25, 25)
            d.rectangle([max(x, bx0) + 1, y + 1, min(x + bw - 1, bx1), y + bh - 1],
                        fill=(150 + shade, 70 + shade // 2, 55 + shade // 2))
    d.rectangle([int(w * 0.30), int(h * 0.25), int(w * 0.40), int(h * 0.38)], fill=(40, 50, 70))  # window
    # fence pickets
    for x in range(0, w, max(8, w // 60)):
        d.rectangle([x, horizon - h // 14, x + max(3, w // 160), horizon + h // 12], fill=(200, 195, 180))
    # gravel speckle
    for _ in range(w * h // 40):
        x, y = rng.randrange(w), rng.randrange(horizon + h // 12, h)
        g = rng.randint(70, 170)
        d.point((x, y), fill=(g, g - 8, g - 20))
    img = ImageChops.add(img, Image.merge("RGB", [_noise_layer(size, rng, 6)] * 3), 1.0, -128)
    if night:
        grey = img.convert("L").point(lambda v: int(v * 0.35 + 8))
        img = Image.merge("RGB", [grey, grey, grey])
    return img


def shifted(img: Image.Image, dx_frac: float, dy_frac: float) -> Image.Image:
    w, h = img.size
    dx, dy = round(dx_frac * w), round(dy_frac * h)
    pad = Image.new("RGB", (w + 2 * abs(dx), h + 2 * abs(dy)))
    px, py = abs(dx), abs(dy)
    pad.paste(img, (px, py))
    # edge replicate: stretch the border row/column strips
    if px:
        pad.paste(img.crop((0, 0, 1, h)).resize((px, h)), (0, py))
        pad.paste(img.crop((w - 1, 0, w, h)).resize((px, h)), (px + w, py))
    if py:
        pad.paste(pad.crop((0, py, pad.width, py + 1)).resize((pad.width, py)), (0, 0))
        pad.paste(pad.crop((0, py + h - 1, pad.width, py + h)).resize((pad.width, py)), (0, py + h))
    return pad.crop((px - dx, py - dy, px - dx + w, py - dy + h))


def blur(img: Image.Image, radius: float) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(radius)) if radius > 0 else img.copy()


def add_glare(img: Image.Image, cx: float, cy: float, radius_frac: float, strength: float = 1.0) -> Image.Image:
    w, h = img.size
    r = radius_frac * w
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse([cx * w - r, cy * h - r, cx * w + r, cy * h + r], fill=int(255 * min(1.0, strength)))
    mask = mask.filter(ImageFilter.GaussianBlur(r * 0.15))
    return Image.composite(Image.new("RGB", img.size, (255, 255, 255)), img, mask)


def add_noise(img: Image.Image, sigma: float, seed: int = 1) -> Image.Image:
    layer = _noise_layer(img.size, random.Random(seed), sigma)
    return ImageChops.add(img, Image.merge("RGB", [layer] * 3), 1.0, -128)


def expose(img: Image.Image, gain: float, offset: float = 0) -> Image.Image:
    return img.point(lambda v: max(0, min(255, int(v * gain + offset))))


def jpeg(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()
