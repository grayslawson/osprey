"""Survey geometry: coordinates, the sweep grid, framing a region, and time estimates. Pure, no I/O.

Positions are the controller's normalised ones: pan and tilt -1..1 (tilt up is positive), zoom 0..1.
Degrees come from the camera's measured geometry. Azimuth and elevation are relative to the middle
of the mechanical travel, not to north or the horizon.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

MIN_OVERLAP: Final = 0.15
MAX_OVERLAP: Final = 0.60
MAX_CELLS: Final = 400


@dataclass(frozen=True)
class Geometry:
    pan_deg: float
    tilt_deg: float
    wide: tuple[float, float]   # horizontal, vertical field of view at zoom 0
    tele: tuple[float, float]   # ... and at zoom 1

    @classmethod
    def from_state(cls, raw: Any) -> Geometry | None:
        """The ``ptz.fov`` block of the state document, or None when the camera is not calibrated."""
        if not isinstance(raw, dict):
            return None
        try:
            geo = cls(float(raw["pan_degrees"]), float(raw["tilt_degrees"]),
                      (float(raw["wide_horizontal_degrees"]), float(raw["wide_vertical_degrees"])),
                      (float(raw["tele_horizontal_degrees"]), float(raw["tele_vertical_degrees"])))
        except (KeyError, TypeError, ValueError):
            return None
        return geo if geo.valid() else None

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> Geometry:
        geo = cls(float(raw["pan_degrees"]), float(raw["tilt_degrees"]),
                  (float(raw["wide"][0]), float(raw["wide"][1])), (float(raw["tele"][0]), float(raw["tele"][1])))
        if not geo.valid():
            raise ValueError("survey.fov needs positive degrees")
        return geo

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Geometry:
        """The shape ``as_dict`` writes."""
        return cls(float(raw["pan_deg"]), float(raw["tilt_deg"]), (float(raw["wide"][0]), float(raw["wide"][1])),
                   (float(raw["tele"][0]), float(raw["tele"][1])))

    def valid(self) -> bool:
        nums = (self.pan_deg, self.tilt_deg, *self.wide, *self.tele)
        return all(math.isfinite(n) and n > 0 for n in nums) and self.wide[0] <= 180 and self.wide[1] <= 180

    def as_dict(self) -> dict[str, Any]:
        return {"pan_deg": self.pan_deg, "tilt_deg": self.tilt_deg, "wide": list(self.wide), "tele": list(self.tele)}

    # normalised <-> degrees
    def az(self, pan: float) -> float:
        return pan * self.pan_deg / 2

    def el(self, tilt: float) -> float:
        return tilt * self.tilt_deg / 2

    def pan_of(self, az: float) -> float:
        return max(-1.0, min(1.0, az * 2 / self.pan_deg))

    def tilt_of(self, el: float) -> float:
        return max(-1.0, min(1.0, el * 2 / self.tilt_deg))

    def fov(self, zoom: float) -> tuple[float, float]:
        z = max(0.0, min(1.0, zoom))
        return (self.wide[0] + z * (self.tele[0] - self.wide[0]), self.wide[1] + z * (self.tele[1] - self.wide[1]))

    def zoom_for(self, h_deg: float, v_deg: float) -> float:
        """The largest zoom whose view still holds an h by v degree box."""
        best = 0.0
        for z in (i / 100 for i in range(101)):
            fh, fv = self.fov(z)
            if fh >= h_deg and fv >= v_deg:
                best = z
        return best

    def extent(self, zoom: float = 0.0) -> tuple[float, float, float, float]:
        """Azimuth and elevation covered by the whole sweep: (az0, el0, az1, el1)."""
        fh, fv = self.fov(zoom)
        return (-(self.pan_deg + fh) / 2, -(self.tilt_deg + fv) / 2, (self.pan_deg + fh) / 2, (self.tilt_deg + fv) / 2)


@dataclass(frozen=True)
class Cell:
    i: int
    col: int
    row: int
    pan: float
    tilt: float
    zoom: float
    az: float
    el: float

    def as_dict(self) -> dict[str, Any]:
        return {"i": self.i, "col": self.col, "row": self.row, "pan": round(self.pan, 5), "tilt": round(self.tilt, 5),
                "zoom": self.zoom, "az": round(self.az, 3), "el": round(self.el, 3)}


def grid_size(geo: Geometry, overlap: float, zoom: float) -> tuple[int, int]:
    if not MIN_OVERLAP <= overlap <= MAX_OVERLAP:
        raise ValueError(f"overlap must be {MIN_OVERLAP} to {MAX_OVERLAP}")
    fh, fv = geo.fov(zoom)
    cols = math.ceil(geo.pan_deg / (fh * (1 - overlap)) - 1e-9) + 1
    rows = math.ceil(geo.tilt_deg / (fv * (1 - overlap)) - 1e-9) + 1
    if cols * rows > MAX_CELLS:
        raise ValueError(f"a {cols} by {rows} grid is more than {MAX_CELLS} cells")
    return cols, rows


def plan(geo: Geometry, overlap: float = 0.25, zoom: float = 0.0, start: tuple[float, float] | None = None) -> list[Cell]:
    """Cells in visiting order: a snake that begins at the corner nearest to ``start`` (pan, tilt)."""
    cols, rows = grid_size(geo, overlap, zoom)
    pans = [-1 + 2 * c / (cols - 1) for c in range(cols)]
    tilts = [1 - 2 * r / (rows - 1) for r in range(rows)]
    sp, st = start if start is not None else (-1.0, 1.0)
    if abs(st - tilts[-1]) < abs(st - tilts[0]):
        tilts = tilts[::-1]
    flip = abs(sp - pans[-1]) < abs(sp - pans[0])
    cells: list[Cell] = []
    for r, tilt in enumerate(tilts):
        row_pans = list(enumerate(pans))
        if flip != (r % 2 == 1):
            row_pans = row_pans[::-1]
        for c, pan in row_pans:
            cells.append(Cell(len(cells), c, r, pan, tilt, zoom, geo.az(pan), geo.el(tilt)))
    return cells


def frame_view(geo: Geometry, az0: float, el0: float, az1: float, el1: float, *, fill: float = 0.7,
               zoom_max: float = 0.7) -> tuple[float, float, float]:
    """Pan, tilt and zoom that frame an azimuth/elevation box so it fills about ``fill`` of the picture."""
    h, v = max(az1 - az0, 1.0) / fill, max(el1 - el0, 1.0) / fill
    zoom = min(zoom_max, geo.zoom_for(h, v))
    return geo.pan_of((az0 + az1) / 2), geo.tilt_of((el0 + el1) / 2), zoom


def to_angles(x: float, y: float, fov: tuple[float, float], projection: str = "rectilinear") -> tuple[float, float]:
    """Picture position (x right, y up, -1..1 at the edges) to angle offsets in degrees."""
    if projection == "linear":
        return x * fov[0] / 2, y * fov[1] / 2
    return (math.degrees(math.atan(x * math.tan(math.radians(fov[0] / 2)))),
            math.degrees(math.atan(y * math.tan(math.radians(fov[1] / 2)))))


def from_angles(dx: float, dy: float, fov: tuple[float, float], projection: str = "rectilinear") -> tuple[float, float]:
    if projection == "linear":
        return dx * 2 / fov[0], dy * 2 / fov[1]
    return (math.tan(math.radians(dx)) / math.tan(math.radians(fov[0] / 2)),
            math.tan(math.radians(dy)) / math.tan(math.radians(fov[1] / 2)))


def travel_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Time-relevant head travel between two (az, el) points: the pan and tilt motors run together."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


# The planning constant for a full-speed sweep: degrees per second at the
# camera's configured ceiling. The estimate scales it by the survey speed.
BASE_DEG_PER_SEC: Final[float] = 30.0


@dataclass(frozen=True)
class Timing:
    deg_per_sec: float = BASE_DEG_PER_SEC
    settle_sec: float = 2.0
    zoom_settle_sec: float = 4.0
    snapshot_sec: float = 0.5


def estimate_sec(geo: Geometry, cells: list[Cell], zoom_ins: int = 0, timing: Timing = Timing(),
                 start: tuple[float, float] | None = None) -> dict[str, float]:
    """Camera time for the sweep, and for ``zoom_ins`` closer looks (a typical hop of a quarter of the range)."""
    pos = start if start is not None else (cells[0].az, cells[0].el) if cells else (0.0, 0.0)
    sweep = 0.0
    for cell in cells:
        here = (cell.az, cell.el)
        sweep += travel_deg(pos, here) / timing.deg_per_sec + timing.settle_sec + timing.snapshot_sec
        pos = here
    hop = max(geo.pan_deg, geo.tilt_deg) / 4
    zoom = zoom_ins * (hop / timing.deg_per_sec + timing.zoom_settle_sec + timing.snapshot_sec)
    return {"sweep_sec": round(sweep, 1), "zoom_sec": round(zoom, 1), "total_sec": round(sweep + zoom, 1)}
