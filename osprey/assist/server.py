"""Assist's own small HTTP API. Only Osprey's console proxy and the owner's scripts call it.

Every request needs the shared bearer token. The proxy adds ``X-Osprey-Principal: console``,
which is recorded with each approval. The listener binds to loopback unless the config says otherwise.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from .engine import Engine, Refused
from .survey_run import Refused as SurveyRefused
from .types import MODES

log = logging.getLogger("osprey.assist.http")
MAX_BODY: Final = 4096
_DECISION = re.compile(r"/decisions/(\d{1,12})(?:/(approve|reject|snooze|undo))?")
_FRAME = re.compile(r"/frames/([0-9]{8}f?_[0-9a-f]{8})\.jpg")
_REASONS: Final = ("not_a_problem", "wrong_fix", "looks_worse", "dont_touch")
_PROPOSAL = re.compile(r"/survey/proposals/(\d{1,12})(?:/(accept|reject|goto|preview\.jpg))?")


class AssistServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], engine: Engine, token_file: str) -> None:
        super().__init__(address, _Handler)
        self.engine = engine
        self.token_file = token_file

    def token(self) -> str:
        try:
            return Path(self.token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def start(self) -> None:
        threading.Thread(target=self.serve_forever, name="assist-http", daemon=True).start()


class _Handler(BaseHTTPRequestHandler):
    server: AssistServer
    timeout = 30

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature fixed by http.server
        log.debug(format, *args)

    def _send(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, body: object) -> None:
        self._send(status, json.dumps(body, separators=(",", ":")).encode())

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            raise Refused("request body is too large")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            raise Refused("request body is not valid JSON") from None
        if not isinstance(data, dict):
            raise Refused("request body must be a JSON object")
        return data

    def _route(self, method: str) -> None:
        supplied = self.headers.get("Authorization", "")
        token = self.server.token()
        if not token or not supplied.lower().startswith("bearer ") or not hmac.compare_digest(
                supplied[7:].strip().encode(), token.encode()):
            self.close_connection = True
            self._error(HTTPStatus.UNAUTHORIZED, "bad_token", "a valid token is required")
            return
        principal = self.headers.get("X-Osprey-Principal", "token")[:32] or "token"
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        engine = self.server.engine
        path = url.path
        try:
            if path.startswith("/survey/"):
                self._survey(engine, method, path, query, principal)
            elif method == "GET" and path in ("/status", "/settings"):
                self._json(200, engine.status())
            elif method == "GET" and path == "/decisions":
                self._list(engine, query)
            elif method == "GET" and (m := _FRAME.fullmatch(path)):
                frame = engine.store.frame(m.group(1))
                if frame is None:
                    self._error(404, "not_found", "no such frame")
                else:
                    self._send(200, frame, "image/jpeg")
            elif (m := _DECISION.fullmatch(path)) and method in ("GET", "POST"):
                self._decision(engine, method, int(m.group(1)), m.group(2), principal)
            elif method == "POST" and path == "/settings":
                self._settings(engine)
            elif method == "POST" and path == "/check":
                data = self._body()
                camera = str(data.get("camera") or next(iter(engine.cams)))
                if camera not in engine.cams:
                    raise Refused("unknown camera")
                found = engine.check(camera, "manual")
                self._json(200, {"ok": True, "decision": found})
            elif method == "DELETE" and path == "/data":
                engine.purge()
                self._json(200, {"ok": True})
            else:
                self._error(404, "not_found", "no such Assist resource")
        except (Refused, SurveyRefused) as exc:
            self._error(409, "refused", str(exc))
        except Exception:  # noqa: BLE001 - never leak internals to the caller
            log.exception("assist request failed")
            self._error(500, "internal", "Assist could not do that")

    def _camera(self, engine: Engine, query: dict[str, str], data: dict[str, Any] | None = None) -> str:
        camera = str((data or {}).get("camera") or query.get("camera") or next(iter(engine.cams)))
        if camera not in engine.cams:
            raise Refused("unknown camera")
        return camera

    def _survey(self, engine: Engine, method: str, path: str, query: dict[str, str], principal: str) -> None:
        svc = engine.survey
        db = svc.db
        if method == "GET":
            camera = self._camera(engine, query)
            if path == "/survey/status":
                self._json(200, svc.status(camera))
                return
            if path == "/survey/maps":
                self._json(200, {"maps": [{k: m[k] for k in ("id", "run", "kind", "ts", "coverage", "seams")}
                                          for m in db.maps(camera)]})
                return
            if path == "/survey/proposals":
                st = query.get("status")
                rows = db.proposals(camera=camera, statuses=tuple(st.split(",")) if st and st != "all" else None)
                self._json(200, {"proposals": rows})
                return
            if (m := _PROPOSAL.fullmatch(path)) and m.group(2) == "preview.jpg":
                row = db.proposal(int(m.group(1)))
                smap = db.map(row["map"]) if row else None
                img = svc.preview(smap, row["pan"], row["tilt"], row["zoom"]) if smap and row and "pan" in row else None
                self._send(200, img, "image/jpeg") if img else self._error(404, "not_found", "no preview")
                return
            if (m := _PROPOSAL.fullmatch(path)) and not m.group(2):
                row = db.proposal(int(m.group(1)))
                self._json(200, row) if row else self._error(404, "not_found", "no such proposal")
                return
            kind = query.get("kind", "day")
            if kind not in ("day", "night"):
                raise Refused("kind must be day or night")
            smap = db.latest_map(camera, kind)
            if smap is None:
                self._error(404, "not_found", f"no {kind} map yet")
                return
            if path == "/survey/map":
                self._json(200, {**smap, "proposals": db.proposals(map_id=smap["id"])})
            elif path == "/survey/panorama.jpg":
                self._send(200, db.read_file(smap["run"], "panorama.jpg") or b"", "image/jpeg")
            elif path == "/survey/overlay.png":
                self._send(200, db.read_file(smap["run"], "overlay.png") or b"", "image/png")
            elif path == "/survey/frigate.json":
                self._send(200, db.read_file(smap["run"], "frigate-suggestions.json") or b"{}")
            elif path == "/survey/preview.jpg":
                try:
                    pan, tilt, zoom = (float(query.get(k, "0")) for k in ("pan", "tilt", "zoom"))
                except ValueError:
                    raise Refused("pan, tilt and zoom must be numbers") from None
                img = svc.preview(smap, max(-1.0, min(1.0, pan)), max(-1.0, min(1.0, tilt)), zoom)
                self._send(200, img, "image/jpeg") if img else self._error(404, "not_found", "no panorama")
            else:
                self._error(404, "not_found", "no such Survey resource")
            return
        data = self._body()
        camera = self._camera(engine, query, data)
        if path == "/survey/start":
            self._json(200, {"ok": True, "run": svc.start(camera, "manual")})
        elif path == "/survey/cancel":
            svc.cancel()
            self._json(200, {"ok": True})
        elif path == "/survey/resume":
            self._json(200, {"ok": True, "run": svc.resume(camera)})
        elif path == "/survey/apply":
            ids = data.get("ids")
            if ids is not None and not (isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
                raise Refused("ids must be a list of proposal numbers")
            if svc.mode() in ("off", "observe"):
                raise Refused(f"survey is in {svc.mode()} mode; nothing is applied")
            self._json(200, svc.apply(camera, ids if ids is not None else
                                      [p["id"] for p in db.proposals(camera=camera, statuses=("pending",)) if p["type"] != "frigate"],
                                      principal))
        elif path == "/survey/revert":
            self._json(200, svc.revert(camera, principal))
        elif (m := _PROPOSAL.fullmatch(path)) and m.group(2) in ("accept", "reject", "goto"):
            pid = int(m.group(1))
            if m.group(2) == "accept":
                self._json(200, svc.accept(pid, principal))
            elif m.group(2) == "reject":
                self._json(200, svc.reject(pid, principal))
            else:
                svc.goto(pid)
                self._json(200, {"ok": True})
        else:
            self._error(404, "not_found", "no such Survey resource")

    def _list(self, engine: Engine, query: dict[str, str]) -> None:
        status = query.get("status", "all")
        statuses = None if status == "all" else tuple(s for s in status.split(",") if s)
        try:
            limit = int(query.get("limit", "50"))
            before = int(query["before"]) if "before" in query else None
        except ValueError:
            raise Refused("limit and before must be numbers") from None
        rows = engine.store.decisions(statuses=statuses, camera=query.get("camera"), limit=limit, before=before)
        self._json(200, {"decisions": [engine.view(r) for r in rows]})

    def _decision(self, engine: Engine, method: str, decision_id: int, action: str | None, principal: str) -> None:
        if method == "GET" and action is None:
            row = engine.store.decision(decision_id)
            if row is None:
                self._error(404, "not_found", "no such decision")
            else:
                self._json(200, engine.view(row))
            return
        if method != "POST" or action is None:
            self._error(405, "method", "unsupported method")
            return
        data = self._body()
        if action == "approve":
            row = engine.approve(decision_id, principal)
        elif action == "reject":
            reason = str(data.get("reason", ""))
            if reason and reason not in _REASONS:
                raise Refused(f"reason must be one of {', '.join(_REASONS)}")
            row = engine.reject(decision_id, principal, reason, str(data.get("note", "")))
        elif action == "snooze":
            row = engine.snooze(decision_id, principal)
        else:
            row = engine.undo(decision_id, principal)
        self._json(200, engine.view(row))

    def _settings(self, engine: Engine) -> None:
        data = self._body()
        unknown = set(data) - {"enabled", "capabilities"}
        if unknown:
            raise Refused(f"unknown setting(s): {', '.join(sorted(unknown))}")
        caps = data.get("capabilities") or {}
        if not isinstance(caps, dict):
            raise Refused("capabilities must be an object")
        for name, body in caps.items():
            mode = body.get("mode") if isinstance(body, dict) else None
            if mode not in MODES:
                raise Refused(f"mode must be one of {', '.join(MODES)}")
            if name not in ("quality", "survey"):
                raise Refused(f"{name} arrives in a later release")
        if "enabled" in data:
            if not isinstance(data["enabled"], bool):
                raise Refused("enabled must be true or false")
            engine.set_enabled(data["enabled"])
        for name, body in caps.items():
            engine.set_mode(name, body["mode"])
        self._json(200, engine.status())

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")
