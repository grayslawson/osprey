"""Polygon helpers in relative (0..1) coordinates. Pillow only: masks are lists of rows of booleans."""

from __future__ import annotations

from collections.abc import Sequence

from PIL import Image, ImageChops, ImageDraw

from .types import Point, Poly

Mask = Sequence[Sequence[bool]]  # rows of booleans; a numpy bool array fits


def clamp(v: float) -> float:
    return min(1.0, max(0.0, v))


def fmt_coords(poly: Poly, places: int = 3) -> str:
    """Frigate's coordinate string: "x1,y1,x2,y2,..." (relative)."""
    return ",".join(f"{round(clamp(v), places):g}" for p in poly for v in p)


def parse_coords(text: str | list[str]) -> Poly:
    raw = text if isinstance(text, str) else ",".join(text)
    nums = [float(t) for t in raw.replace(" ", "").split(",") if t]
    if len(nums) % 2:
        raise ValueError("odd number of coordinates")
    return list(zip(nums[0::2], nums[1::2], strict=True))


def area(poly: Poly) -> float:
    s = 0.0
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1], strict=False):
        s += x0 * y1 - x1 * y0
    return abs(s) / 2


def bbox(poly: Poly) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def is_valid(poly: Poly, min_area: float = 0.0005) -> bool:
    """At least 3 distinct points, inside the frame, with real area."""
    if len(poly) < 3 or len(set(poly)) < 3:
        return False
    if any(not (0 <= x <= 1 and 0 <= y <= 1) for x, y in poly):
        return False
    return area(poly) >= min_area


def rasterize(poly: Poly, w: int, h: int) -> Image.Image:
    """The polygon as a mode "L" picture, 1 inside and 0 outside."""
    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).polygon([(x * (w - 1), y * (h - 1)) for x, y in poly], fill=1)
    return img


def _count(img: Image.Image) -> int:
    return img.histogram()[1]


def coverage(polys: list[Poly], res: int = 160) -> float:
    """Fraction of the frame covered by the union of polygons."""
    if not polys:
        return 0.0
    m = Image.new("L", (res, res), 0)
    for p in polys:
        m = ImageChops.lighter(m, rasterize(p, res, res))
    return _count(m) / (res * res)


def overlap_fraction(a: Poly, b: Poly, res: int = 160) -> float:
    """Share of a that lies inside b."""
    ma, mb = rasterize(a, res, res), rasterize(b, res, res)
    n = _count(ma)
    return _count(ImageChops.darker(ma, mb)) / n if n else 0.0


def iou(a: Poly, b: Poly, res: int = 160) -> float:
    ma, mb = rasterize(a, res, res), rasterize(b, res, res)
    union = _count(ImageChops.lighter(ma, mb))
    return _count(ImageChops.darker(ma, mb)) / union if union else 0.0


def _dp(pts: list[Point], eps: float) -> list[Point]:
    if len(pts) < 3:
        return pts
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = (dx * dx + dy * dy) ** 0.5 or 1e-12
    dmax, idx = 0.0, 0
    for i in range(1, len(pts) - 1):
        d = abs(dy * (pts[i][0] - x0) - dx * (pts[i][1] - y0)) / norm
        if d > dmax:
            dmax, idx = d, i
    if dmax <= eps:
        return [pts[0], pts[-1]]
    return _dp(pts[: idx + 1], eps)[:-1] + _dp(pts[idx:], eps)


def simplify(poly: Poly, eps: float = 0.008) -> Poly:
    """Douglas-Peucker for a closed ring."""
    if len(poly) <= 4:
        return poly
    far = max(range(len(poly)), key=lambda i: (poly[i][0] - poly[0][0]) ** 2 + (poly[i][1] - poly[0][1]) ** 2)
    a = _dp(poly[: far + 1], eps)
    b = _dp(poly[far:] + poly[:1], eps)
    return a[:-1] + b[:-1]


def _components(mask: Mask) -> list[list[tuple[int, int]]]:
    h, w = len(mask), len(mask[0]) if len(mask) else 0
    seen = [[False] * w for _ in range(h)]
    out: list[list[tuple[int, int]]] = []
    for y0 in range(h):
        for x0 in range(w):
            if seen[y0][x0] or not mask[y0][x0]:
                continue
            stack, comp = [(x0, y0)], []
            seen[y0][x0] = True
            while stack:
                x, y = stack.pop()
                comp.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            out.append(comp)
    return out


def _outer_ring(comp: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Boundary of a 4-connected pixel blob as a clockwise vertex ring (holes are ignored)."""
    cells = set(comp)
    nxt: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for x, y in comp:
        if (x, y - 1) not in cells:
            nxt.setdefault((x, y), []).append((x + 1, y))
        if (x + 1, y) not in cells:
            nxt.setdefault((x + 1, y), []).append((x + 1, y + 1))
        if (x, y + 1) not in cells:
            nxt.setdefault((x + 1, y + 1), []).append((x, y + 1))
        if (x - 1, y) not in cells:
            nxt.setdefault((x, y + 1), []).append((x, y))
    best: list[tuple[int, int]] = []
    best_area = 0.0
    while nxt:
        start = next(iter(nxt))
        ring, cur = [start], start
        while True:
            v = cur
            cur = nxt[v].pop()
            if not nxt[v]:
                del nxt[v]
            if cur == start or cur not in nxt:
                break
            ring.append(cur)
        signed = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=False)) / 2
        if signed > best_area:  # outer boundary is positive in image coordinates; holes are negative
            best, best_area = ring, signed
    return best


def mask_to_polygons(mask: Mask, eps: float = 0.008, min_area: float = 0.004, max_n: int = 4) -> list[Poly]:
    """Outer boundaries of the largest connected blobs of a boolean mask, as simplified relative polygons."""
    h, w = len(mask), len(mask[0]) if len(mask) else 0
    polys: list[tuple[float, Poly]] = []
    for comp in _components(mask):
        if len(comp) / (h * w) < min_area:
            continue
        ring = _outer_ring(comp)
        if len(ring) < 3:
            continue
        rel = simplify([(x / w, y / h) for x, y in ring], eps)
        if len(rel) >= 3:
            polys.append((area(rel), rel))
    polys.sort(key=lambda t: -t[0])
    return [p for _, p in polys[:max_n]]
