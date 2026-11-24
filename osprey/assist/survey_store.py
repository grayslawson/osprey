"""Survey's records: runs, captures, maps, proposals, learned heat and a short position log.

SQLite rows live in the same database as the rest of Assist; pictures and map files live in
``<state>/survey/<run id>/``. Retention is here too: raw captures expire sooner than maps.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Final

from .store import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS survey_runs(id INTEGER PRIMARY KEY, camera TEXT, ts REAL, kind TEXT, status TEXT,
  trigger TEXT, updated REAL, body TEXT);
CREATE TABLE IF NOT EXISTS survey_cells(run_id INTEGER, idx INTEGER, kind TEXT, ts REAL, frame TEXT, body TEXT,
  PRIMARY KEY(run_id, idx));
CREATE TABLE IF NOT EXISTS survey_maps(id INTEGER PRIMARY KEY, run_id INTEGER, camera TEXT, kind TEXT, ts REAL,
  body TEXT);
CREATE TABLE IF NOT EXISTS survey_proposals(id INTEGER PRIMARY KEY, map_id INTEGER, camera TEXT, type TEXT,
  status TEXT, ts REAL, body TEXT);
CREATE INDEX IF NOT EXISTS survey_proposals_status ON survey_proposals(status, camera);
CREATE TABLE IF NOT EXISTS survey_heat(camera TEXT, bx INTEGER, by INTEGER, weight REAL, updated REAL,
  PRIMARY KEY(camera, bx, by));
CREATE TABLE IF NOT EXISTS survey_pos(ts REAL, camera TEXT, pan REAL, tilt REAL, zoom REAL);
CREATE INDEX IF NOT EXISTS survey_pos_ts ON survey_pos(camera, ts);
"""
OPEN_STATUSES: Final = ("queued", "running", "paused", "analysing")
POS_KEEP_DAYS: Final = 30


class SurveyStore:
    def __init__(self, store: Store) -> None:
        self.s = store
        store._db.executescript(_SCHEMA)
        self.root = store.dir / "survey"
        self.root.mkdir(exist_ok=True, mode=0o700)

    # ---------------------------------------------------------------- runs
    def add_run(self, camera: str, kind: str, trigger: str, body: dict[str, Any]) -> int:
        now = self.s.clock()
        cur = self.s._run("INSERT INTO survey_runs(camera,ts,kind,status,trigger,updated,body) VALUES(?,?,?,?,?,?,?)",
                          (camera, now, kind, "queued", trigger, now, json.dumps(body)))
        run_id = int(cur.lastrowid or 0)
        self.run_dir(run_id)
        return run_id

    def run_dir(self, run_id: int) -> Path:
        path = self.root / str(int(run_id))
        path.mkdir(exist_ok=True, mode=0o700)
        return path

    def update_run(self, run_id: int, status: str | None = None, **body: Any) -> None:
        with self.s._lock:
            row = self.s._db.execute("SELECT body FROM survey_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return
            merged = {**json.loads(row["body"]), **body}
            if status is not None:
                self.s._db.execute("UPDATE survey_runs SET status=?, updated=?, body=? WHERE id=?",
                                   (status, self.s.clock(), json.dumps(merged), run_id))
            else:
                self.s._db.execute("UPDATE survey_runs SET updated=?, body=? WHERE id=?",
                                   (self.s.clock(), json.dumps(merged), run_id))

    @staticmethod
    def _run_row(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "camera": row["camera"], "ts": row["ts"], "kind": row["kind"],
                "status": row["status"], "trigger": row["trigger"], "updated": row["updated"], **json.loads(row["body"])}

    def run(self, run_id: int) -> dict[str, Any] | None:
        row = self.s._run("SELECT * FROM survey_runs WHERE id=?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def runs(self, camera: str | None = None, statuses: tuple[str, ...] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        if camera:
            where.append("camera=?")
            args.append(camera)
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += list(statuses)
        rows = self.s._run(f"SELECT * FROM survey_runs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",  # noqa: S608
                           (*args, limit)).fetchall()
        return [self._run_row(r) for r in rows]

    def runs_since(self, camera: str, seconds: float) -> int:
        return int(self.s._run("SELECT COUNT(*) AS c FROM survey_runs WHERE camera=? AND ts>? AND trigger!='resume'",
                               (camera, self.s.clock() - seconds)).fetchone()["c"])

    # --------------------------------------------------------------- cells
    def save_cell(self, run_id: int, idx: int, kind: str, jpeg: bytes | None, body: dict[str, Any]) -> None:
        name = None
        if jpeg:
            name = f"cells/{idx:03d}.jpg"
            path = self.run_dir(run_id) / name
            path.parent.mkdir(exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(jpeg)
        self.s._run("INSERT OR REPLACE INTO survey_cells VALUES(?,?,?,?,?,?)",
                    (run_id, idx, kind, self.s.clock(), name, json.dumps(body)))

    def cells(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.s._run("SELECT * FROM survey_cells WHERE run_id=? ORDER BY idx", (run_id,)).fetchall()
        return [{"idx": r["idx"], "kind": r["kind"], "ts": r["ts"], "frame": r["frame"], **json.loads(r["body"])} for r in rows]

    def cell_jpeg(self, run_id: int, name: str | None) -> bytes | None:
        if not name:
            return None
        try:
            return (self.run_dir(run_id) / name).read_bytes()
        except OSError:
            return None

    # ---------------------------------------------------------------- maps
    def write_file(self, run_id: int, name: str, data: bytes) -> None:
        path = self.run_dir(run_id) / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)

    def read_file(self, run_id: int, name: str) -> bytes | None:
        try:
            return (self.run_dir(run_id) / name).read_bytes()
        except OSError:
            return None

    def add_map(self, run_id: int, camera: str, kind: str, body: dict[str, Any]) -> int:
        cur = self.s._run("INSERT INTO survey_maps(run_id,camera,kind,ts,body) VALUES(?,?,?,?,?)",
                          (run_id, camera, kind, self.s.clock(), json.dumps(body)))
        return int(cur.lastrowid or 0)

    @staticmethod
    def _map_row(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "run": row["run_id"], "camera": row["camera"], "kind": row["kind"], "ts": row["ts"],
                **json.loads(row["body"])}

    def map(self, map_id: int) -> dict[str, Any] | None:
        row = self.s._run("SELECT * FROM survey_maps WHERE id=?", (map_id,)).fetchone()
        return self._map_row(row) if row else None

    def latest_map(self, camera: str, kind: str) -> dict[str, Any] | None:
        row = self.s._run("SELECT * FROM survey_maps WHERE camera=? AND kind=? ORDER BY id DESC LIMIT 1",
                          (camera, kind)).fetchone()
        return self._map_row(row) if row else None

    def maps(self, camera: str, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.s._run("SELECT * FROM survey_maps WHERE camera=? ORDER BY id DESC LIMIT ?", (camera, limit)).fetchall()
        return [self._map_row(r) for r in rows]

    def update_map(self, map_id: int, **body: Any) -> None:
        with self.s._lock:
            row = self.s._db.execute("SELECT body FROM survey_maps WHERE id=?", (map_id,)).fetchone()
            if row is not None:
                self.s._db.execute("UPDATE survey_maps SET body=? WHERE id=?",
                                   (json.dumps({**json.loads(row["body"]), **body}), map_id))

    # ----------------------------------------------------------- proposals
    def add_proposal(self, map_id: int, camera: str, kind: str, body: dict[str, Any], status: str = "pending") -> int:
        cur = self.s._run("INSERT INTO survey_proposals(map_id,camera,type,status,ts,body) VALUES(?,?,?,?,?,?)",
                          (map_id, camera, kind, status, self.s.clock(), json.dumps(body)))
        return int(cur.lastrowid or 0)

    @staticmethod
    def _prop(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "map": row["map_id"], "camera": row["camera"], "type": row["type"],
                "status": row["status"], "ts": row["ts"], **json.loads(row["body"])}

    def proposal(self, pid: int) -> dict[str, Any] | None:
        row = self.s._run("SELECT * FROM survey_proposals WHERE id=?", (pid,)).fetchone()
        return self._prop(row) if row else None

    def proposals(self, *, map_id: int | None = None, camera: str | None = None,
                  statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        for col, value in (("map_id", map_id), ("camera", camera)):
            if value is not None:
                where.append(f"{col}=?")
                args.append(value)
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += list(statuses)
        rows = self.s._run(f"SELECT * FROM survey_proposals WHERE {' AND '.join(where)} ORDER BY id",  # noqa: S608
                           tuple(args)).fetchall()
        return [self._prop(r) for r in rows]

    def update_proposal(self, pid: int, status: str | None = None, **body: Any) -> None:
        with self.s._lock:
            row = self.s._db.execute("SELECT body FROM survey_proposals WHERE id=?", (pid,)).fetchone()
            if row is None:
                return
            merged = json.dumps({**json.loads(row["body"]), **body})
            if status is None:
                self.s._db.execute("UPDATE survey_proposals SET body=? WHERE id=?", (merged, pid))
            else:
                self.s._db.execute("UPDATE survey_proposals SET status=?, body=? WHERE id=?", (status, merged, pid))

    def count_pending(self, camera: str | None = None) -> int:
        if camera:
            row = self.s._run("SELECT COUNT(*) AS c FROM survey_proposals WHERE status='pending' AND camera=? "
                              "AND type!='frigate'", (camera,)).fetchone()
        else:
            row = self.s._run("SELECT COUNT(*) AS c FROM survey_proposals WHERE status='pending' "
                              "AND type!='frigate'").fetchone()
        return int(row["c"])

    # ---------------------------------------------------------------- heat
    def add_heat(self, camera: str, bx: int, by: int, amount: float, half_life_days: float) -> None:
        now = self.s.clock()
        row = self.s._run("SELECT weight, updated FROM survey_heat WHERE camera=? AND bx=? AND by=?",
                          (camera, bx, by)).fetchone()
        old = float(row["weight"]) * 0.5 ** ((now - float(row["updated"])) / (half_life_days * 86400)) if row else 0.0
        self.s._run("INSERT OR REPLACE INTO survey_heat VALUES(?,?,?,?,?)", (camera, bx, by, old + amount, now))

    def heat(self, camera: str, half_life_days: float) -> dict[tuple[int, int], float]:
        """Decayed weights per block, scaled so the busiest block is 1. Empty when nothing has been learned."""
        now = self.s.clock()
        rows = self.s._run("SELECT bx, by, weight, updated FROM survey_heat WHERE camera=?", (camera,)).fetchall()
        raw = {(r["bx"], r["by"]): float(r["weight"]) * 0.5 ** ((now - float(r["updated"])) / (half_life_days * 86400))
               for r in rows}
        top = max(raw.values(), default=0.0)
        return {k: v / top for k, v in raw.items() if top > 0 and v / top > 0.001}

    # ------------------------------------------------------------ position
    def log_position(self, camera: str, pan: float, tilt: float, zoom: float) -> None:
        last = self.s._run("SELECT pan, tilt, zoom FROM survey_pos WHERE camera=? ORDER BY ts DESC LIMIT 1",
                           (camera,)).fetchone()
        if last and (abs(last["pan"] - pan) < 1e-3 and abs(last["tilt"] - tilt) < 1e-3 and abs(last["zoom"] - zoom) < 1e-3):
            return
        self.s._run("INSERT INTO survey_pos VALUES(?,?,?,?,?)", (self.s.clock(), camera, pan, tilt, zoom))

    def position_at(self, camera: str, ts: float, within: float = 6 * 3600) -> tuple[float, float, float] | None:
        """Where the camera was at ``ts``: the last logged position at or before it, if not too old."""
        row = self.s._run("SELECT ts, pan, tilt, zoom FROM survey_pos WHERE camera=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                          (camera, ts)).fetchone()
        return (row["pan"], row["tilt"], row["zoom"]) if row is not None and ts - row["ts"] <= within else None

    # ----------------------------------------------------------- retention
    def cleanup(self, *, keep_maps: int, frames_days: float) -> int:
        """Drop the raw captures of old runs and the whole folder of maps beyond the newest ``keep_maps`` per kind."""
        now = self.s.clock()
        removed = 0
        keep_runs: set[int] = set()
        for camera, kind in self.s._run("SELECT DISTINCT camera, kind FROM survey_maps").fetchall():
            rows = self.s._run("SELECT id, run_id FROM survey_maps WHERE camera=? AND kind=? ORDER BY id DESC",
                               (camera, kind)).fetchall()
            keep_runs |= {r["run_id"] for r in rows[:keep_maps]}
            for r in rows[keep_maps:]:
                self.s._run("DELETE FROM survey_maps WHERE id=?", (r["id"],))
        for open_run in self.runs(statuses=(*OPEN_STATUSES, "interrupted"), limit=50):
            keep_runs.add(open_run["id"])
        for path in self.root.iterdir():
            if not path.is_dir() or not path.name.isdigit():
                continue
            run_id = int(path.name)
            if run_id not in keep_runs and now - path.stat().st_mtime > 3600:
                shutil.rmtree(path, ignore_errors=True)
                self.s._run("DELETE FROM survey_cells WHERE run_id=?", (run_id,))
                removed += 1
            elif run_id in keep_runs and now - path.stat().st_mtime > frames_days * 86400:
                cells = path / "cells"
                if cells.is_dir():
                    shutil.rmtree(cells, ignore_errors=True)
                    removed += 1
        self.s._run("DELETE FROM survey_pos WHERE ts<?", (now - POS_KEEP_DAYS * 86400,))
        self.s._run("DELETE FROM survey_proposals WHERE ts<? AND status IN ('rejected','expired','reverted','failed')",
                    (now - 90 * 86400,))
        return removed

    def purge(self) -> None:
        with self.s._lock:
            for table in ("survey_runs", "survey_cells", "survey_maps", "survey_proposals", "survey_heat", "survey_pos"):
                self.s._db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(exist_ok=True, mode=0o700)
