"""Image-quality numbers from one JPEG snapshot. Pillow C paths only."""

from __future__ import annotations

import io
import statistics

from PIL import Image, ImageChops, ImageFilter, ImageStat

from .types import GRID_H, GRID_W, Metrics

_WIDTH = 960
_LAPLACIAN = ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), 1, 128)
_HOT = 250


def _pct(hist: list[int], total: int, q: float) -> int:
    acc, target = 0, total * q
    for v, n in enumerate(hist):
        acc += n
        if acc >= target:
            return v
    return 255


def _boxes(w: int, h: int) -> list[tuple[int, int, int, int]]:
    xs = [round(i * w / GRID_W) for i in range(GRID_W + 1)]
    ys = [round(j * h / GRID_H) for j in range(GRID_H + 1)]
    return [(xs[i], ys[j], xs[i + 1], ys[j + 1]) for j in range(GRID_H) for i in range(GRID_W)]


def _largest_cluster(hot: list[bool]) -> list[int]:
    seen: set[int] = set()
    best: list[int] = []
    for start, is_hot in enumerate(hot):
        if not is_hot or start in seen:
            continue
        seen.add(start)
        stack, comp = [start], []
        while stack:
            c = stack.pop()
            comp.append(c)
            x, y = c % GRID_W, c // GRID_W
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                n = ny * GRID_W + nx
                if 0 <= nx < GRID_W and 0 <= ny < GRID_H and hot[n] and n not in seen:
                    seen.add(n)
                    stack.append(n)
        if len(comp) > len(best):
            best = comp
    return best


def compute(jpeg: bytes) -> Metrics:
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("L")
    except Exception as exc:  # Pillow raises many types for bad input
        raise ValueError(f"undecodable image: {exc}") from exc
    if img.width != _WIDTH:
        img = img.resize((_WIDTH, max(GRID_H, round(img.height * _WIDTH / img.width))), Image.Resampling.BILINEAR)

    n = img.width * img.height
    hist = img.histogram()
    clip_hi = sum(hist[_HOT:]) / n
    clip_lo = sum(hist[:6]) / n
    luma = ImageStat.Stat(img).mean[0]
    contrast = _pct(hist, n, 0.95) - _pct(hist, n, 0.05)

    # shortcut: offset 128 clips Laplacian/high-pass values beyond +-128; fine for variance ranking
    lap = img.filter(_LAPLACIAN)
    hp = ImageChops.subtract(img, img.filter(ImageFilter.BoxBlur(1)), 1.0, 128)
    # shortcut: tile Laplacian variance doubles as the gradient measure for picking flat tiles
    hotmask = img.point([0] * _HOT + [255] * (256 - _HOT))

    boxes = _boxes(img.width, img.height)
    lap_var = [ImageStat.Stat(lap.crop(b)).var[0] for b in boxes]
    hot_frac = [ImageStat.Stat(hotmask.crop(b)).mean[0] / 255 for b in boxes]

    order = sorted(range(len(boxes)), key=lambda i: lap_var[i])
    top = [lap_var[i] for i in order[len(order) // 2:]]
    sharp = statistics.median(top)
    flat = order[: max(1, len(order) // 5)]
    noise = statistics.median(ImageStat.Stat(hp.crop(boxes[i])).stddev[0] for i in flat)

    cluster = _largest_cluster([f > 0.3 for f in hot_frac])
    if cluster:
        glare = len(cluster) / (GRID_W * GRID_H)
        cx = sum((c % GRID_W + 0.5) for c in cluster) / len(cluster) / GRID_W
        cy = sum((c // GRID_W + 0.5) for c in cluster) / len(cluster) / GRID_H
        gcx, gcy = round(cx, 4), round(cy, 4)
    else:
        glare, gcx, gcy = 0.0, None, None

    return Metrics(
        sharp=round(sharp, 4), clip_hi=round(clip_hi, 4), clip_lo=round(clip_lo, 4),
        luma=round(luma, 4), contrast=float(contrast), noise=round(noise, 4),
        glare=round(glare, 4), glare_cx=gcx, glare_cy=gcy,
        glare_map=tuple(min(9, int(f * 10)) for f in hot_frac),
    )
