"""Assist's own state: SQLite for the log and the learning, JPEG files for the frames.

Nothing here lives in Osprey's state directory and the controller never reads it. The
directory is 0700 and the files are 0600. Learning is plain counting and moving
averages in these tables, so a decision can be replayed from them.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, ts REAL, camera TEXT, preset TEXT, scene TEXT,
  hour INTEGER, source TEXT, metrics TEXT, frame TEXT);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS baselines(camera TEXT, preset TEXT, scene TEXT, hour INTEGER, metric TEXT,
  n INTEGER, mean REAL, var REAL, updated REAL, PRIMARY KEY(camera, preset, scene, hour, metric));
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, ts REAL, camera TEXT, capability TEXT, preset TEXT,
  scene TEXT, trigger TEXT, problem TEXT, confidence REAL, source TEXT, status TEXT, structural INTEGER,
  expires_at REAL, resolved_at REAL, before_frame TEXT, after_frame TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS decisions_status ON decisions(status, ts);
CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, decision_id INTEGER, ts REAL, principal TEXT,
  verdict TEXT, reason TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS levers(camera TEXT, preset TEXT, scene TEXT, problem TEXT, lever TEXT,
  good INTEGER DEFAULT 0, bad INTEGER DEFAULT 0, cooldown_until REAL DEFAULT 0, last_dir INTEGER DEFAULT 0,
  flips TEXT DEFAULT '[]', frozen INTEGER DEFAULT 0, PRIMARY KEY(camera, preset, scene, problem, lever));
CREATE TABLE IF NOT EXISTS profiles(camera TEXT, preset TEXT, scene TEXT, body TEXT,
  PRIMARY KEY(camera, preset, scene));
CREATE TABLE IF NOT EXISTS ledger(ts REAL, camera TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS ledger_ts ON ledger(ts);
CREATE TABLE IF NOT EXISTS model_calls(ts REAL, provider TEXT, model TEXT, task TEXT, ms REAL, tokens_in INTEGER,
  tokens_out INTEGER, cost REAL, ok INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""
_NAME = re.compile(r"^[0-9]{8}[a-z]?_[0-9a-f]{8}$")


class Store:
    def __init__(self, directory: str | os.PathLike[str], clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(directory)
        self.clock = clock
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.dir, 0o700)
        self.frames_dir = self.dir / "frames"
        self.frames_dir.mkdir(exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        path = self.dir / "assist.db"
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(str(path) + suffix, 0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _run(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default: Any = None) -> Any:
        row = self._run("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        self._run("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, json.dumps(value)))

    # -------------------------------------------------------------- frames
    def save_frame(self, jpeg: bytes, kind: str = "q") -> str:
        """Write a JPEG and return its name. ``kind`` is ``q`` (quality) or ``f`` (follow-up, people)."""
        now = time.gmtime(self.clock())
        name = f"{time.strftime('%Y%m%d', now)}{kind if kind != 'q' else ''}_{secrets.token_hex(4)}"
        folder = self.frames_dir / time.strftime("%Y-%m-%d", now)
        folder.mkdir(exist_ok=True, mode=0o700)
        path = folder / f"{name}.jpg"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(jpeg)
        return name

    def frame(self, name: str) -> bytes | None:
        path = self.frame_path(name)
        try:
            return path.read_bytes() if path is not None else None
        except OSError:
            return None

    def frame_path(self, name: str) -> Path | None:
        if not _NAME.fullmatch(name):
            return None
        stamp = name[:8]
        return self.frames_dir / f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" / f"{name}.jpg"

    # ------------------------------------------------------------- samples
    def add_sample(self, camera: str, preset: str, scene: str, source: str, metrics: dict[str, Any],
                   frame: str | None = None) -> int:
        now = self.clock()
        hour = time.localtime(now).tm_hour
        cur = self._run("INSERT INTO samples(ts,camera,preset,scene,hour,source,metrics,frame) VALUES(?,?,?,?,?,?,?,?)",
                        (now, camera, preset, scene, hour, source, json.dumps(metrics), frame))
        return int(cur.lastrowid or 0)

    # ----------------------------------------------------------- baselines
    def baseline(self, camera: str, preset: str, scene: str, hour: int, metric: str) -> tuple[int, float, float] | None:
        row = self._run("SELECT n,mean,var FROM baselines WHERE camera=? AND preset=? AND scene=? AND hour=? AND metric=?",
                        (camera, preset, scene, hour, metric)).fetchone()
        return (int(row["n"]), float(row["mean"]), float(row["var"])) if row else None

    def set_baseline(self, camera: str, preset: str, scene: str, hour: int, metric: str,
                     n: int, mean: float, var: float) -> None:
        self._run("INSERT OR REPLACE INTO baselines VALUES(?,?,?,?,?,?,?,?,?)",
                  (camera, preset, scene, hour, metric, n, mean, var, self.clock()))

    # ----------------------------------------------------------- decisions
    def add_decision(self, row: dict[str, Any], body: dict[str, Any]) -> int:
        cur = self._run(
            "INSERT INTO decisions(ts,camera,capability,preset,scene,trigger,problem,confidence,source,status,"
            "structural,expires_at,before_frame,after_frame,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.clock(), row["camera"], row.get("capability", "quality"), row["preset"], row["scene"], row["trigger"],
             row["problem"], row["confidence"], row["source"], row["status"], int(bool(row.get("structural"))),
             row.get("expires_at"), row.get("before_frame"), row.get("after_frame"), json.dumps(body)))
        return int(cur.lastrowid or 0)

    def update_decision(self, decision_id: int, **fields: Any) -> None:
        body_patch = fields.pop("body", None)
        with self._lock:
            if body_patch:
                row = self._db.execute("SELECT body FROM decisions WHERE id=?", (decision_id,)).fetchone()
                if row is not None:
                    merged = {**json.loads(row["body"]), **body_patch}
                    fields["body"] = json.dumps(merged)
            if "structural" in fields:
                fields["structural"] = int(bool(fields["structural"]))
            if not fields:
                return
            names = ", ".join(f"{k}=?" for k in fields)
            self._db.execute(f"UPDATE decisions SET {names} WHERE id=?", (*fields.values(), decision_id))  # noqa: S608

    def decision(self, decision_id: int) -> dict[str, Any] | None:
        row = self._run("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return self._decision(row) if row else None

    def decisions(self, *, statuses: tuple[str, ...] | None = None, camera: str | None = None,
                  limit: int = 50, before: int | None = None) -> list[dict[str, Any]]:
        where: list[str] = ["1=1"]
        args: list[Any] = []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += list(statuses)
        if camera:
            where.append("camera=?")
            args.append(camera)
        if before:
            where.append("id<?")
            args.append(before)
        rows = self._run(f"SELECT * FROM decisions WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",  # noqa: S608
                         (*args, max(1, min(limit, 500)))).fetchall()
        return [self._decision(r) for r in rows]

    def count_decisions(self, status: str) -> int:
        return int(self._run("SELECT COUNT(*) AS c FROM decisions WHERE status=?", (status,)).fetchone()["c"])

    @staticmethod
    def _decision(row: sqlite3.Row) -> dict[str, Any]:
        out = {k: row[k] for k in row.keys() if k != "body"}  # noqa: SIM118 - sqlite3.Row
        out["structural"] = bool(out["structural"])
        out.update(json.loads(row["body"]))
        return out

    # ------------------------------------------------------------ feedback
    def add_feedback(self, decision_id: int, principal: str, verdict: str, reason: str = "", note: str = "") -> None:
        self._run("INSERT INTO feedback(decision_id,ts,principal,verdict,reason,note) VALUES(?,?,?,?,?,?)",
                  (decision_id, self.clock(), principal, verdict, reason, note[:500]))

    def feedback_for(self, camera: str, preset: str, problem: str, limit: int = 3) -> list[dict[str, Any]]:
        rows = self._run(
            "SELECT f.verdict, f.reason, d.scene, d.body FROM feedback f JOIN decisions d ON d.id=f.decision_id "
            "WHERE d.camera=? AND d.preset=? AND d.problem=? ORDER BY f.id DESC LIMIT ?",
            (camera, preset, problem, limit)).fetchall()
        return [{"verdict": r["verdict"], "reason": r["reason"], "scene": r["scene"],
                 "change": json.loads(r["body"]).get("change", "")} for r in rows]

    def last_feedback(self, decision_id: int) -> dict[str, Any] | None:
        """The newest person's answer on one decision, ignoring the engine's own 'apply' marks."""
        row = self._run("SELECT verdict, reason FROM feedback WHERE decision_id=? AND verdict!='apply' "
                        "ORDER BY id DESC LIMIT 1", (decision_id,)).fetchone()
        return {"verdict": row["verdict"], "reason": row["reason"]} if row else None

    def reject_count(self, camera: str, preset: str, problem: str) -> int:
        return int(self._run(
            "SELECT COUNT(*) AS c FROM feedback f JOIN decisions d ON d.id=f.decision_id WHERE d.camera=? "
            "AND d.preset=? AND d.problem=? AND f.verdict='reject' AND f.reason='not_a_problem'",
            (camera, preset, problem)).fetchone()["c"])

    # -------------------------------------------------------------- levers
    def lever(self, camera: str, preset: str, scene: str, problem: str, lever: str) -> dict[str, Any]:
        row = self._run("SELECT * FROM levers WHERE camera=? AND preset=? AND scene=? AND problem=? AND lever=?",
                        (camera, preset, scene, problem, lever)).fetchone()
        if row is None:
            return {"good": 0, "bad": 0, "cooldown_until": 0.0, "last_dir": 0, "flips": [], "frozen": False}
        out = {k: row[k] for k in row.keys()}  # noqa: SIM118
        out["flips"] = json.loads(out["flips"])
        out["frozen"] = bool(out["frozen"])
        return out

    def save_lever(self, camera: str, preset: str, scene: str, problem: str, lever: str, state: dict[str, Any]) -> None:
        self._run("INSERT OR REPLACE INTO levers VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (camera, preset, scene, problem, lever, state["good"], state["bad"], state["cooldown_until"],
                   state["last_dir"], json.dumps(state["flips"]), int(state["frozen"])))

    def blocked_levers(self, camera: str, preset: str, scene: str, problem: str) -> frozenset[str]:
        now = self.clock()
        rows = self._run("SELECT lever FROM levers WHERE camera=? AND preset=? AND scene=? AND problem=? "
                         "AND (cooldown_until>? OR frozen=1)", (camera, preset, scene, problem, now)).fetchall()
        return frozenset(r["lever"] for r in rows)

    # ------------------------------------------------------------ profiles
    def profile(self, camera: str, preset: str, scene: str) -> dict[str, float]:
        row = self._run("SELECT body FROM profiles WHERE camera=? AND preset=? AND scene=?",
                        (camera, preset, scene)).fetchone()
        return dict(json.loads(row["body"])) if row else {}

    def set_profile(self, camera: str, preset: str, scene: str, body: dict[str, float]) -> None:
        self._run("INSERT OR REPLACE INTO profiles VALUES(?,?,?,?)", (camera, preset, scene, json.dumps(body)))

    def profile_keys(self, camera: str) -> set[str]:
        keys: set[str] = set()
        for row in self._run("SELECT body FROM profiles WHERE camera=?", (camera,)).fetchall():
            keys |= set(json.loads(row["body"]))
        return keys

    # -------------------------------------------------------------- ledger
    def spend(self, camera: str, kind: str) -> None:
        self._run("INSERT INTO ledger VALUES(?,?,?)", (self.clock(), camera, kind))

    def spent(self, camera: str, kind: str, window_sec: float) -> int:
        return int(self._run("SELECT COUNT(*) AS c FROM ledger WHERE camera=? AND kind=? AND ts>?",
                             (camera, kind, self.clock() - window_sec)).fetchone()["c"])

    # --------------------------------------------------------- model calls
    def log_call(self, rec: dict[str, Any]) -> None:
        self._run("INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (self.clock(), rec.get("provider"), rec.get("model"), rec.get("task"), rec.get("ms"),
                   rec.get("tokens_in"), rec.get("tokens_out"), rec.get("cost"), int(bool(rec.get("ok"))), rec.get("error")))

    def calls_today(self) -> int:
        return int(self._run("SELECT COUNT(*) AS c FROM model_calls WHERE ts>?", (self.clock() - 86400,)).fetchone()["c"])

    # ----------------------------------------------------------- retention
    def cleanup(self, *, frames_days: float, followup_days: float, samples_days: float, decisions_days: float,
                max_frames_mb: int, pending_grace_days: float = 7) -> dict[str, int]:
        """Drop what has outlived its retention. Frames of pending or recently resolved suggestions stay."""
        now = self.clock()
        keep: set[str] = set()
        cutoff = now - pending_grace_days * 86400
        for row in self._run("SELECT before_frame, after_frame FROM decisions WHERE status='pending' "
                             "OR (resolved_at IS NOT NULL AND resolved_at>?) OR (resolved_at IS NULL AND ts>?)",
                             (cutoff, cutoff)).fetchall():
            keep |= {n for n in (row["before_frame"], row["after_frame"]) if n}
        removed = 0
        entries: list[tuple[float, Path]] = []
        for path in self.frames_dir.glob("*/*.jpg"):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            limit = (followup_days if re.fullmatch(r"\d{8}f_[0-9a-f]{8}", path.stem) else frames_days) * 86400
            if path.stem in keep:
                entries.append((path.stat().st_mtime, path))
                continue
            if age > limit:
                path.unlink(missing_ok=True)
                removed += 1
            else:
                entries.append((path.stat().st_mtime, path))
        total = sum(p.stat().st_size for _, p in entries if p.exists())
        for _, path in sorted(entries):  # oldest first, never a pending suggestion's frames
            if total <= max_frames_mb * 1024 * 1024:
                break
            if path.stem in keep:
                continue
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
            removed += 1
        for folder in self.frames_dir.iterdir():
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        cur1 = self._run("DELETE FROM samples WHERE ts<?", (now - samples_days * 86400,))
        cur2 = self._run("DELETE FROM decisions WHERE ts<? AND status NOT IN ('pending','applied')",
                         (now - decisions_days * 86400,))
        self._run("DELETE FROM model_calls WHERE ts<?", (now - decisions_days * 86400,))
        self._run("DELETE FROM ledger WHERE ts<?", (now - 8 * 86400,))
        free = self._run("PRAGMA freelist_count").fetchone()[0]
        total_pages = self._run("PRAGMA page_count").fetchone()[0]
        if total_pages and free / total_pages > 0.2:
            self._run("VACUUM")
        return {"frames": removed, "samples": cur1.rowcount, "decisions": cur2.rowcount}

    def purge(self) -> None:
        """Forget everything: frames, the log and the learning."""
        with self._lock:
            for table in ("samples", "baselines", "decisions", "feedback", "levers", "profiles", "ledger",
                          "model_calls"):
                self._db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
            self._db.execute("VACUUM")
        shutil.rmtree(self.frames_dir, ignore_errors=True)
        self.frames_dir.mkdir(exist_ok=True, mode=0o700)
