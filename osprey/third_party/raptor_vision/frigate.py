"""Safe Frigate config writer shared by Kestrel and Osprey. See docs/frigate-writer.md.

Stdlib only. Read-only unless ``Writer(..., allow_write=True)``. ONE writer per Frigate (Kestrel if present, else Osprey).
Endpoints (Frigate 0.18, frigate/api/app.py): GET /api/version, /api/config, /api/config/raw, /api/config/schema.json;
PUT /api/config/set (JSON body: config_data, requires_restart, update_topic); POST /api/config/save?save_option=...
(text body, whole YAML).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SUPPORTED_VERSIONS = ("0.18", "0.17")  # current stable + previous; anything else is refused
NAMED_MASKS_FROM = (0, 18)  # before this, masks were bare coordinate lists: zones only
DEFAULT_PREFIX = "rv_"  # items we create carry this id prefix; everything else is "not ours"
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CAM = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

Kind = Literal["zone", "motion_mask", "object_mask"]
_SECTION = {"zone": "zones", "motion_mask": "motion", "object_mask": "objects"}


class FrigateError(Exception):
    """Network, HTTP or protocol failure."""


class WriteRefused(FrigateError):
    """A safety rule said no (read-only, version, locked, foreign, stale, approval)."""


class FrigateClient:
    """HTTP client. ``get`` is public; the only write path is the private ``_send``, used by Writer."""

    def __init__(self, base_url: str, token_file: str | None = None, timeout: float = 20) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("frigate url must be http(s)")
        self.base, self.timeout, self.token_file = base_url.rstrip("/"), timeout, token_file

    def _headers(self) -> dict[str, str]:
        if not self.token_file:
            return {}
        try:
            return {"Authorization": f"Bearer {Path(self.token_file).read_text().strip()}"}
        except OSError as exc:
            raise FrigateError("token file unreadable") from exc

    def _open(self, method: str, path: str, params: dict[str, str] | None, data: bytes | None, ctype: str | None) -> bytes:
        q = urllib.parse.urlencode(params or {})
        headers = self._headers()
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(f"{self.base}{path}{'?' + q if q else ''}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out: bytes = r.read()
                return out
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", "replace") if exc.fp else ""
            raise FrigateError(f"http {exc.code} for {method} {path}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FrigateError(f"network: {type(exc).__name__}") from None

    def get(self, path: str, **params: Any) -> bytes:
        return self._open("GET", path, {k: str(v) for k, v in params.items() if v is not None}, None, None)

    def _send(self, method: str, path: str, params: dict[str, str], body: dict[str, Any] | str) -> bytes:
        if isinstance(body, str):
            return self._open(method, path, params, body.encode(), "text/plain")
        return self._open(method, path, params, json.dumps(body).encode(), "application/json")

    def json(self, path: str, **params: Any) -> Any:
        try:
            return json.loads(self.get(path, **params))
        except ValueError:
            raise FrigateError(f"non-json reply for {path}") from None

    def version(self) -> str:
        return self.get("/api/version").decode().strip()

    def config(self) -> dict[str, Any]:
        d = self.json("/api/config")
        if not isinstance(d, dict):
            raise FrigateError("config is not an object")
        return d

    def schema(self) -> dict[str, Any]:
        d = self.json("/api/config/schema.json")
        if not isinstance(d, dict):
            raise FrigateError("schema is not an object")
        return d

    def raw_config(self) -> str:
        """The YAML on disk (admin only). Frigate sends it as a JSON string; a plain body is accepted too."""
        text = self.get("/api/config/raw").decode()
        if text.startswith('"'):
            try:
                v = json.loads(text)
                if isinstance(v, str):
                    return v
            except ValueError:
                pass
        return text


@dataclass(frozen=True)
class Change:
    """One zone or mask on one camera. ``name`` is the final Frigate id; without the writer's prefix it counts as foreign.

    ``coordinates`` are relative (0..1) polygon points. ``privacy=True`` marks a mask that hides a private area
    (neighbour, window): it always needs ``approved_by``. ``allow_foreign=True`` lets you modify an item we did not create.
    """

    kind: Kind
    camera: str
    name: str
    coordinates: tuple[tuple[float, float], ...]
    objects: tuple[str, ...] = ()  # zones only
    friendly_name: str | None = None
    enabled: bool = True
    privacy: bool = False
    allow_foreign: bool = False

    def fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {"coordinates": ",".join(f"{round(v, 4):g}" for p in self.coordinates for v in p), "enabled": self.enabled}
        if self.friendly_name:
            out["friendly_name"] = self.friendly_name
        if self.kind == "zone" and self.objects:
            out["objects"] = list(self.objects)
        return out


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    params: dict[str, str]
    body: dict[str, Any] | str | None
    note: str = ""


@dataclass(frozen=True)
class Expect:
    """Read-back check: ``fields=None`` means the item must be absent."""

    kind: Kind
    camera: str
    name: str
    fields: dict[str, Any] | None


@dataclass(frozen=True)
class Plan:
    base: str
    version: str
    raw_hash: str | None
    changes: tuple[Change, ...]
    calls: tuple[Call, ...]
    inverse: tuple[Call, ...]
    expect: tuple[Expect, ...]
    undo_expect: tuple[Expect, ...]
    diff: str
    needs_approval: tuple[str, ...]  # human reasons; non-empty means apply() needs approved_by


@dataclass(frozen=True)
class RollbackHandle:
    base: str
    backup_path: str
    inverse: tuple[Call, ...]
    undo_expect: tuple[Expect, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> RollbackHandle:
        d = json.loads(text)
        return cls(d["base"], d["backup_path"], tuple(Call(**c) for c in d["inverse"]), tuple(Expect(**e) for e in d["undo_expect"]))


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    approved_by: str | None
    backup_path: str
    handle: RollbackHandle
    messages: list[str] = field(default_factory=list)


def _coords(v: Any) -> tuple[float, ...]:
    parts = [v] if isinstance(v, str) else list(v or [])
    try:
        return tuple(round(float(x), 4) for s in parts for x in str(s).split(",") if x.strip())
    except ValueError:
        return ()


def _norm(key: str, v: Any) -> Any:
    if key == "coordinates":
        return _coords(v)
    if key == "objects":
        return tuple(sorted(v)) if isinstance(v, list) else ()
    return v


def _items(cfg: dict[str, Any], kind: Kind, cam: str) -> dict[str, Any] | None:
    c = cfg.get("cameras", {}).get(cam)
    if not isinstance(c, dict):
        return None
    node = c.get("zones") if kind == "zone" else (c.get("motion" if kind == "motion_mask" else "objects") or {}).get("mask")
    return node if isinstance(node, dict) else {}


def _minor(version: str) -> tuple[int, int] | None:
    m = re.match(r"(\d+)\.(\d+)", version)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _config_data(kind: Kind, cam: str, items: dict[str, Any]) -> dict[str, Any]:
    inner: dict[str, Any]
    inner = {"zones": items} if kind == "zone" else {"motion" if kind == "motion_mask" else "objects": {"mask": items}}
    return {"cameras": {cam: inner}}


def _is_locked(name: str, item: dict[str, Any] | None) -> bool:
    if name.lower().endswith("_locked"):
        return True
    return bool(item) and "[locked]" in str((item or {}).get("friendly_name") or "").lower()


class Writer:
    """Plan, apply and roll back zone/mask changes. Create one Writer per Frigate, per process."""

    def __init__(
        self,
        client: FrigateClient,
        *,
        allow_write: bool = False,
        supported: tuple[str, ...] = SUPPORTED_VERSIONS,
        prefix: str = DEFAULT_PREFIX,
        locked: tuple[str, ...] = (),
    ) -> None:
        self.client, self.allow_write, self.supported, self.prefix, self.locked = client, allow_write, supported, prefix, set(locked)
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(client.base, threading.Lock())

    # -- planning -----------------------------------------------------------------------------------------------
    def plan(self, changes: list[Change]) -> Plan:
        if not changes:
            raise ValueError("no changes")
        version = self.client.version()
        minor = _minor(version)
        if minor is None or f"{minor[0]}.{minor[1]}" not in self.supported:
            raise WriteRefused(f"Frigate {version!r} is not a supported version ({', '.join(self.supported)})")
        try:
            raw: str | None = self.client.raw_config()
        except FrigateError:
            raw = None  # not admin: planning works, apply() will refuse
        cfg = self.client.config()
        problems: list[str] = []
        approval: list[str] = []
        groups: dict[tuple[Kind, str], dict[str, Any]] = {}
        undo: dict[tuple[Kind, str], dict[str, Any]] = {}
        expect: list[Expect] = []
        undo_expect: list[Expect] = []
        lines: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for ch in changes:
            tag = f"{ch.camera}/{ch.name}"
            if not _CAM.match(ch.camera) or not _NAME.match(ch.name) or len(ch.coordinates) < 3:
                problems.append(f"{tag}: bad camera/name or fewer than 3 points")
                continue
            if any(not (0 <= x <= 1 and 0 <= y <= 1) for x, y in ch.coordinates):
                problems.append(f"{tag}: coordinates must be relative (0..1)")
                continue
            if (ch.kind, ch.camera, ch.name) in seen:
                problems.append(f"{tag}: listed twice")
                continue
            seen.add((ch.kind, ch.camera, ch.name))
            if ch.camera in self.locked or tag in self.locked:
                problems.append(f"{tag}: locked by the caller")
                continue
            existing = _items(cfg, ch.kind, ch.camera)
            if existing is None:
                problems.append(f"{ch.camera}: no such camera")
                continue
            if ch.kind != "zone" and minor < NAMED_MASKS_FROM:
                problems.append(f"{tag}: masks are only written on Frigate {NAMED_MASKS_FROM[0]}.{NAMED_MASKS_FROM[1]}+ (zones are fine)")
                continue
            old = existing.get(ch.name)
            if _is_locked(ch.name, old if isinstance(old, dict) else None):
                problems.append(f"{tag}: marked locked, never modified")
                continue
            if old is not None and not ch.name.startswith(self.prefix) and not ch.allow_foreign:
                problems.append(f"{tag}: exists and was not created by us (needs allow_foreign=True)")
                continue
            if ch.privacy:
                approval.append(f"{tag}: privacy mask")
            new = ch.fields()
            if old is None:
                lines.append(f"+ {ch.kind} {tag}: " + ", ".join(f"{k}={v}" for k, v in new.items()))
                undo.setdefault((ch.kind, ch.camera), {})[ch.name] = None
                undo_expect.append(Expect(ch.kind, ch.camera, ch.name, None))
            else:
                diffs = [f"{k}: {old.get(k)!r} -> {v!r}" for k, v in new.items() if _norm(k, old.get(k)) != _norm(k, v)]
                if not diffs:
                    continue
                lines.append(f"~ {ch.kind} {tag}: " + "; ".join(diffs))
                prior = {k: old.get(k) for k in new if old.get(k) is not None}
                undo.setdefault((ch.kind, ch.camera), {})[ch.name] = prior
                undo_expect.append(Expect(ch.kind, ch.camera, ch.name, prior))
            groups.setdefault((ch.kind, ch.camera), {})[ch.name] = new
            expect.append(Expect(ch.kind, ch.camera, ch.name, new))
        if problems:
            raise WriteRefused("; ".join(problems))
        if not groups:
            raise WriteRefused("nothing to change: the live config already matches")

        def calls(src: dict[tuple[Kind, str], dict[str, Any]], what: str) -> tuple[Call, ...]:
            return tuple(
                Call(
                    "PUT",
                    "/api/config/set",
                    {},
                    {
                        "config_data": _config_data(k, cam, items),
                        "requires_restart": 0,
                        "update_topic": f"config/cameras/{cam}/{_SECTION[k]}",
                    },
                    f"{what} {k} on {cam}",
                )
                for (k, cam), items in src.items()
            )

        return Plan(
            self.client.base,
            version,
            hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None,
            tuple(changes),
            calls(groups, "apply"),
            calls(undo, "undo"),
            tuple(expect),
            tuple(undo_expect),
            "\n".join(lines),
            tuple(approval),
        )

    # -- applying -----------------------------------------------------------------------------------------------
    def apply(self, plan: Plan, *, approved_by: str | None = None, backup_dir: str | Path) -> ApplyResult:
        self._need_write(plan.base)
        if plan.needs_approval and not approved_by:
            raise WriteRefused("approval required: " + "; ".join(plan.needs_approval))
        if plan.raw_hash is None:
            raise WriteRefused("raw config unreadable (admin token needed): cannot back up or detect staleness")
        with self._lock:
            raw = self.client.raw_config()
            if hashlib.sha256(raw.encode()).hexdigest() != plan.raw_hash:
                raise WriteRefused("stale: the Frigate config changed since the plan; plan again")
            d = Path(backup_dir)
            d.mkdir(parents=True, exist_ok=True)
            backup = d / f"frigate-config-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}-{plan.raw_hash[:8]}.yaml"
            with backup.open("x") as f:  # "x": never overwrite an existing backup
                f.write(raw)
            handle = RollbackHandle(plan.base, str(backup), plan.inverse, plan.undo_expect)
            backup.with_suffix(".handle.json").write_text(handle.to_json())
            try:
                for c in plan.calls:
                    self._call(c)
                bad = self._verify(plan.expect)
            except FrigateError as exc:
                bad = [str(exc)]
            if bad:
                undone = self._rollback_locked(handle, restart=False)
                state = "verified" if undone else f"FAILED, restore {backup}"
                raise FrigateError(f"apply failed ({'; '.join(bad)}); rollback {state}")
            return ApplyResult(True, approved_by, str(backup), handle)

    def rollback(self, handle: RollbackHandle, *, restart: bool = False) -> bool:
        """Undo an apply. Tries the inverse API calls first (live), then restores the whole backup file. True if verified."""
        self._need_write(handle.base)
        with self._lock:
            return self._rollback_locked(handle, restart=restart)

    # -- internals ----------------------------------------------------------------------------------------------
    def _need_write(self, base: str) -> None:
        if not self.allow_write:
            raise WriteRefused("read-only: construct Writer(..., allow_write=True) to write")
        if base != self.client.base:
            raise WriteRefused("plan or handle belongs to a different Frigate")

    def _call(self, c: Call) -> None:
        assert c.body is not None
        self.client._send(c.method, c.path, c.params, c.body)

    def _verify(self, expects: tuple[Expect, ...]) -> list[str]:
        cfg = self.client.config()
        bad: list[str] = []
        for e in expects:
            item = (_items(cfg, e.kind, e.camera) or {}).get(e.name)
            if e.fields is None:
                ok = item is None
            else:
                ok = isinstance(item, dict) and all(_norm(k, item.get(k)) == _norm(k, v) for k, v in e.fields.items())
            if not ok:
                bad.append(f"read-back mismatch for {e.camera}/{e.name}")
        return bad

    def _rollback_locked(self, handle: RollbackHandle, *, restart: bool) -> bool:
        try:
            for c in handle.inverse:
                self._call(c)
            if not self._verify(handle.undo_expect):
                return True
        except FrigateError:
            pass
        try:  # fallback: put the whole file back; live state only follows after a restart
            text = Path(handle.backup_path).read_text()
            self.client._send("POST", "/api/config/save", {"save_option": "restart" if restart else "saveonly"}, text)
        except (FrigateError, OSError):
            return False
        try:
            return not self._verify(handle.undo_expect)  # after save-only the live config is unchanged, so this is False
        except FrigateError:
            return False  # restarting
