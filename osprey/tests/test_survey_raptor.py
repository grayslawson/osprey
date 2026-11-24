"""Survey on raptor-vision: whole-picture chat drawing with fallbacks, the rules floor, and the Frigate writer modes."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import assist.config as acfg
from stub_models import HTTP_500, StubModels, chat_body
from test_survey import SRig


def _drawing(body: dict[str, Any]) -> Any:
    """What a drawing model says of any picture: sky on top, a door in the middle, driveway below."""
    if body["model"] == "bad":
        return {"error": "nope"}
    regions = [{"label": "sky", "polygon": [[0, 0], [1, 0], [1, 0.3], [0, 0.3]], "confidence": 0.9},
               {"label": "door", "polygon": [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]], "confidence": 0.8},
               {"label": "driveway", "polygon": [[0, 0.7], [1, 0.7], [1, 1], [0, 1]], "confidence": 0.9}]
    return chat_body({"regions": regions})


TASK = {"mode": "model", "provider": "stub", "model": "m", "send_frames": True, "min_confidence": 0.3}


def test_a_chat_model_draws_whole_pictures_and_the_map_carries_its_labels(tmp_path: Path) -> None:
    with StubModels(chat_reply=_drawing) as models:
        rig = SRig(tmp_path, models=models, chat=True, tasks={"survey.label": TASK})
        try:
            run = rig.run()
            smap = rig.svc.db.map(run["map"])
            assert smap is not None and smap["used_model"] and smap["model_calls"] >= 24
            labels = {r["label"] for r in smap["regions"]}
            assert {"sky", "door", "driveway"} <= labels
            door = next(r for r in smap["regions"] if r["label"] == "door")
            assert door["entry"] > 0.5                      # a drawn door says it is a way in
            assert all(len(r["json"]["messages"][1]["content"]) == 2 for r in models.requests)   # one text, one picture
        finally:
            rig.close()


def test_a_failing_model_falls_back_to_the_next_then_to_the_rules(tmp_path: Path) -> None:
    with StubModels(chat_reply=_drawing) as models:
        task = {**TASK, "model": "bad", "fallback": [{"model": "good"}]}
        rig = SRig(tmp_path, models=models, chat=True, tasks={"survey.label": task})
        try:
            smap = rig.svc.db.map(rig.run()["map"])
            assert smap is not None and "door" in {r["label"] for r in smap["regions"]}
            assert {r["json"]["model"] for r in models.requests} == {"bad", "good"}
        finally:
            rig.close()
    with StubModels(chat_reply=HTTP_500) as models:
        (tmp_path / "b").mkdir()
        rig = SRig(tmp_path / "b", models=models, chat=True, tasks={"survey.label": TASK})
        try:
            run = rig.run()
            assert run["status"] == "ready"                 # the survey still finishes, on the rules floor
            smap = rig.svc.db.map(run["map"])
            assert smap is not None and smap["regions"]
        finally:
            rig.close()


def test_the_vocabulary_keeps_the_old_order_so_stored_label_maps_still_read() -> None:
    from assist.survey_map import LABELS
    assert LABELS[:12] == ("door", "gate", "driveway", "path", "street", "yard", "fence_line", "sky", "wall_eaves",
                           "foliage", "glare_source", "other")
    assert "building" not in LABELS and {"sidewalk", "porch", "timestamp", "neighbour"} <= set(LABELS)


def test_the_task_keeps_ospreys_two_extra_keys() -> None:
    from assist.decide import parse_task
    t = parse_task("t", {"mode": "model", "provider": "p", "model": "m", "on_disagree": "model", "explain": True,
                         "fallback": [{"model": "n"}]})
    assert (t.on_disagree, t.explain, t.fallback) == ("model", True, (("n", None),))
    for bad in ({"on_disagree": "x"}, {"explain": 1}, {"nope": 1}):
        with pytest.raises(ValueError):
            parse_task("t", bad)


def test_a_labeller_with_no_model_names_what_the_rules_can(tmp_path: Path) -> None:
    from assist.survey_understand import label_frame
    from third_party.raptor_vision.types import Frame
    import io
    from PIL import Image
    img = io.BytesIO()
    Image.new("RGB", (320, 180), (90, 150, 230)).save(img, "JPEG")
    regions, calls = label_frame(None, Frame(img.getvalue(), 320, 180, "day", 0.0), "", (4, 3))
    assert calls == 0 and {r.label for r in regions} == {"sky"}  # the whole picture is blue and flat


# ---------------------------------------------------------------- the Frigate writer

class WritableFrigate:
    """Just enough of Frigate 0.18: version, config, raw config and PUT /api/config/set that merges zones."""

    def __init__(self) -> None:
        self.cfg: dict[str, Any] = {"cameras": {"ptz": {"zones": {}}}}
        self.puts: list[dict[str, Any]] = []
        self._srv: ThreadingHTTPServer | None = None
        self.url = ""

    def __enter__(self) -> WritableFrigate:
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/api/version":
                    self._send(b"0.18.3")
                elif path == "/api/config/raw":
                    self._send(json.dumps(json.dumps(stub.cfg, sort_keys=True)).encode())
                elif path == "/api/config":
                    self._send(json.dumps(stub.cfg).encode())
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_PUT(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.puts.append(body)
                for cam, section in body["config_data"]["cameras"].items():
                    zones = stub.cfg["cameras"][cam]["zones"]
                    for name, z in section["zones"].items():
                        if z is None:
                            zones.pop(name, None)
                        else:
                            zones[name] = z
                self._send(b"{}")

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._srv.serve_forever, args=(0.02,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        return self

    def __exit__(self, *exc: object) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()


@pytest.fixture
def frigate() -> Iterator[WritableFrigate]:
    with WritableFrigate() as f:
        yield f


def _survey(tmp: Path, models: StubModels, frigate_cfg: dict[str, Any]) -> SRig:
    return SRig(tmp, models=models, chat=True, tasks={"survey.label": TASK}, survey={"frigate": frigate_cfg})


def _frigate_proposal(rig: SRig, run: dict[str, Any]) -> dict[str, Any]:
    return next(p for p in rig.svc.db.proposals(map_id=run["map"]) if p["type"] == "frigate")


def test_the_default_writes_nothing_and_asks_for_no_copying(tmp_path: Path, frigate: WritableFrigate) -> None:
    with StubModels(chat_reply=_drawing) as models:
        rig = _survey(tmp_path, models, {"url": frigate.url, "camera": "ptz"})
        try:
            p = _frigate_proposal(rig, rig.run())
            assert p["writer"] == "off" and p["status"] == "written" and not frigate.puts
            assert rig.svc.apply_all("default", "t") is not None and not frigate.puts   # Apply all never touches Frigate
        finally:
            rig.close()


def test_osprey_writes_the_zones_only_after_the_console_accepts(tmp_path: Path, frigate: WritableFrigate) -> None:
    with StubModels(chat_reply=_drawing) as models:
        rig = _survey(tmp_path, models, {"url": frigate.url, "camera": "ptz", "writer": "osprey"})
        try:
            p = _frigate_proposal(rig, rig.run())
            assert p["status"] == "pending" and "rv_" in p["diff"] and not frigate.puts
            out = rig.svc.accept(p["id"], "console")
            assert out["applied"] == [p["id"]] and out["failed"] == {}
            names = set(frigate.cfg["cameras"]["ptz"]["zones"])
            assert names and all(n.startswith("rv_") for n in names)
            done = rig.svc.db.proposal(p["id"])
            assert done is not None and done["status"] == "applied" and Path(done["applied"]["backup"]).is_file()
        finally:
            rig.close()


def test_a_stale_frigate_config_fails_the_proposal_and_changes_nothing(tmp_path: Path, frigate: WritableFrigate) -> None:
    with StubModels(chat_reply=_drawing) as models:
        rig = _survey(tmp_path, models, {"url": frigate.url, "camera": "nowhere", "writer": "osprey"})
        try:
            p = _frigate_proposal(rig, rig.run())
            out = rig.svc.accept(p["id"], "console")
            assert out["applied"] == [] and "no such camera" in out["failed"][str(p["id"])] and not frigate.puts
            assert rig.svc.db.proposal(p["id"])["status"] == "failed"   # type: ignore[index]
        finally:
            rig.close()


def test_kestrel_mode_offers_the_zones_over_the_api_and_osprey_never_writes(tmp_path: Path, frigate: WritableFrigate) -> None:
    with StubModels(chat_reply=_drawing) as models:
        rig = _survey(tmp_path, models, {"url": frigate.url, "camera": "ptz", "writer": "kestrel"})
        try:
            p = _frigate_proposal(rig, rig.run())
            assert p["status"] == "offered" and p["suggestion"]["presets"]
            with pytest.raises(Exception, match="not waiting"):
                rig.svc.accept(p["id"], "console")
            assert not frigate.puts
        finally:
            rig.close()


def test_the_writer_setting_is_validated() -> None:
    with pytest.raises(ValueError):
        acfg.parse({"survey": {"frigate": {"url": "http://x", "writer": "both"}}})
    with pytest.raises(ValueError):
        acfg.parse({"survey": {"frigate": {"url": "http://x", "writer": "osprey"}}})      # needs the camera name
    assert acfg.parse({"survey": {"frigate": {"url": "http://x"}}}).survey.frigate.writer == "off"  # type: ignore[union-attr]


def test_the_vendored_library_says_which_release_it_is() -> None:
    import re
    version = (Path(__file__).parent.parent / "third_party" / "raptor_vision" / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
