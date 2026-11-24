from __future__ import annotations

import time

import pytest
from PIL import Image

import assist_scenes as sc
from assist.metrics import compute
from assist.types import GRID_H, GRID_W, Metrics

BASE = sc.scene()


def m(img: Image.Image) -> Metrics:
    return compute(sc.jpeg(img))


def test_blur_lowers_sharp() -> None:
    vals = [m(sc.blur(BASE, r)).sharp for r in (0, 1.5, 4)]
    assert vals[0] > vals[1] > vals[2]


def test_glare_blob_and_centroid() -> None:
    clean = m(BASE)
    assert clean.glare < 0.01 and clean.glare_cx is None
    g = m(sc.add_glare(BASE, 0.3, 0.4, 0.15))
    assert g.glare > 0.05
    assert g.glare_cx is not None and g.glare_cy is not None
    assert abs(g.glare_cx - 0.3) < 0.1 and abs(g.glare_cy - 0.4) < 0.1
    g2 = m(sc.add_glare(BASE, 0.7, 0.6, 0.15))
    assert g2.glare_cx is not None and g2.glare_cy is not None
    assert g2.glare_cx > g.glare_cx + 0.2 and g2.glare_cy > g.glare_cy


def test_noise_increases() -> None:
    assert m(sc.add_noise(BASE, 20, seed=3)).noise > m(BASE).noise


def test_exposure_clipping() -> None:
    base, over, under = m(BASE), m(sc.expose(BASE, 2.5, 40)), m(sc.expose(BASE, 0.3, -40))
    assert over.clip_hi > base.clip_hi and over.luma > base.luma
    assert under.clip_lo > base.clip_lo and under.luma < base.luma


def test_bad_jpeg_raises() -> None:
    with pytest.raises(ValueError):
        compute(b"not a jpeg")


def test_glare_map_shape_and_speed() -> None:
    img = sc.jpeg(sc.add_glare(BASE, 0.5, 0.5, 0.2))
    t = time.perf_counter()
    r = compute(img)
    assert time.perf_counter() - t < 1.0
    assert len(r.glare_map) == GRID_W * GRID_H == 96
    assert all(0 <= v <= 9 for v in r.glare_map) and max(r.glare_map) >= 5
    assert GRID_H == 8
