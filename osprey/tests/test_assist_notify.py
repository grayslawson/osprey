"""Osprey Assist notifications: MQTT discovery, commands, ntfy.  Imports nothing from the controller."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from assist.notify import Callbacks, Notifier, NotifyConfig, parse_notify

P = "osprey/cam1/assist"


class FakeAdapter:
    def __init__(self) -> None:
        self.pubs: list[tuple[str, bytes, bool]] = []
        self.subs: dict[str, Callable[[str, bytes, bool], None]] = {}
        self.will: tuple[str, bytes] | None = None
        self.started = self.stopped = False
        self.fail = False

    def subscribe(self, topic: str, callback: Callable[[str, bytes, bool], None]) -> None:
        self.subs[topic] = callback

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if self.fail:
            raise OSError("broker down")
        self.pubs.append((topic, payload, retain))

    def set_will(self, topic: str, payload: bytes) -> None:
        self.will = (topic, payload)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def send(self, suffix: str, payload: bytes | str | dict[str, Any], retained: bool = False) -> None:
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        data = payload.encode() if isinstance(payload, str) else payload
        topic = f"{P}/command/{suffix}"
        self.subs[topic](topic, data, retained)

    def last(self, topic: str) -> bytes:
        return [p for t, p, _ in self.pubs if t == topic][-1]

    def topics(self) -> list[str]:
        return [t for t, _, _ in self.pubs]


class Rec:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.newest: dict[str, Any] | None = {"id": 7}
        self.raise_on_decide = False

    def callbacks(self) -> Callbacks:
        def decide(i: int, v: str, r: str, who: str) -> None:
            if self.raise_on_decide:
                raise KeyError(i)
            self.calls.append(("decide", i, v, r, who))
        return Callbacks(
            decide=decide,
            set_enabled=lambda b: self.calls.append(("enabled", b)),
            set_mode=lambda c, m: self.calls.append(("mode", c, m)),
            check_now=lambda: self.calls.append(("check",)),
            newest_pending=lambda: self.newest)


DECISION = {"id": 7, "ts": 1.0, "preset": "gate", "scene": "NIGHT", "problem": "glare", "summary": "Glare at the gate",
            "change": "gain 20 -> 12", "status": "pending", "before_frame": "b.jpg", "after_frame": "a.jpg",
            "structural": False, "warning": None}


def make(cfg: NotifyConfig | None = None, **kw: Any) -> tuple[Notifier, FakeAdapter, Rec, list[Any]]:
    ad, rec, posts = FakeAdapter(), Rec(), []
    cfg = cfg or parse_notify({"device": "cam1"})

    def post(url: str, body: bytes, headers: dict[str, str], method: str) -> None:
        posts.append((url, body, headers, method))

    n = Notifier(cfg, ad, rec.callbacks(), lambda name: {"b.jpg": b"BEFORE", "a.jpg": b"AFTER"}.get(name),
                 http_post=post, **kw)
    n.start()
    return n, ad, rec, posts


def test_start_sets_will_subscribes_and_goes_online() -> None:
    n, ad, _, _ = make()
    assert ad.started and ad.will == (f"{P}/availability", b"offline")
    assert ad.last(f"{P}/availability") == b"online"
    assert f"{P}/command/decision" in ad.subs and f"{P}/command/mode/quality" in ad.subs
    n.stop()
    assert ad.stopped and ad.last(f"{P}/availability") == b"offline"


def test_discovery_payloads() -> None:
    _, ad, _, _ = make()
    cfgs = {t: json.loads(p) for t, p, r in ad.pubs if t.startswith("homeassistant/") and r}
    assert len(cfgs) == 17
    comps = sorted(t.split("/")[1] for t in cfgs)
    assert comps == ["button"] * 4 + ["event", "image", "image", "select", "select"] + ["sensor"] * 7 + ["switch"]
    uids = set()
    for t, c in cfgs.items():
        assert c["availability_topic"] == f"{P}/availability"
        assert c["device"]["identifiers"] == ["osprey_cam1_assist"]
        assert t.endswith("/config") and c["unique_id"].startswith("osprey_cam1_assist_")
        uids.add(c["unique_id"])
    assert len(uids) == 17
    sw = cfgs["homeassistant/switch/osprey_cam1_assist/enabled/config"]
    assert sw["command_topic"] == f"{P}/command/enabled" and sw["state_topic"] == f"{P}/state/enabled"
    sel = cfgs["homeassistant/select/osprey_cam1_assist/quality_mode/config"]
    assert sel["options"] == ["off", "observe", "suggest", "act", "auto"]
    assert sel["command_topic"] == f"{P}/command/mode/quality"
    img = cfgs["homeassistant/image/osprey_cam1_assist/suggestion_before/config"]
    assert img["image_topic"] == f"{P}/image/before" and img["content_type"] == "image/jpeg"
    ev = cfgs["homeassistant/event/osprey_cam1_assist/suggestion/config"]
    assert ev["event_types"] == ["suggested", "applied", "reverted", "expired"]
    pend = cfgs["homeassistant/sensor/osprey_cam1_assist/pending/config"]
    assert pend["json_attributes_topic"] == f"{P}/state/pending_attrs"
    assert cfgs["homeassistant/button/osprey_cam1_assist/approve_newest/config"]["command_topic"].endswith("approve_newest")


def test_discovery_off_and_frames_off() -> None:
    _, ad, _, _ = make(parse_notify({"device": "cam1", "ha_discovery": False}))
    assert not [t for t in ad.topics() if t.startswith("homeassistant/")]
    n, _, _, _ = make(parse_notify({"device": "cam1", "mqtt_frames": False}))
    assert not [t for t, c in n.discovery_configs() if "/image/" in t]


def test_decision_command_dispatch() -> None:
    _, ad, rec, _ = make()
    ad.send("decision", {"id": 3, "verdict": "approve", "reason": "ok"})
    ad.send("decision", {"id": 4, "verdict": "reject"})
    assert rec.calls == [("decide", 3, "approve", "ok", "mqtt"), ("decide", 4, "reject", "", "mqtt")]


@pytest.mark.parametrize("payload", [b"not json", b"[1]", b'{"id":"3","verdict":"approve"}',
                                     b'{"id":true,"verdict":"approve"}', b'{"id":1,"verdict":"nuke"}',
                                     b'{"id":1,"verdict":"approve","reason":5}'])
def test_bad_decision_ignored(payload: bytes) -> None:
    _, ad, rec, _ = make()
    ad.send("decision", payload)
    assert rec.calls == []


def test_retained_ignored() -> None:
    _, ad, rec, _ = make()
    ad.send("decision", {"id": 3, "verdict": "approve"}, retained=True)
    ad.send("enabled", "OFF", retained=True)
    ad.send("approve_newest", "PRESS", retained=True)
    assert rec.calls == []


def test_unknown_id_callback_error_is_soft() -> None:
    _, ad, rec, _ = make()
    rec.raise_on_decide = True
    ad.send("decision", {"id": 999, "verdict": "approve"})  # must not raise


def test_switch_select_and_auto_refusal() -> None:
    _, ad, rec, _ = make()
    ad.send("enabled", "OFF")
    ad.send("enabled", "maybe")
    ad.send("mode/quality", "suggest")
    ad.send("mode/quality", "auto")
    ad.send("mode/quality", "bogus")
    ad.send("check", "PRESS")
    assert rec.calls == [("enabled", False), ("mode", "quality", "suggest"), ("check",)]
    _, ad2, rec2, _ = make(parse_notify({"device": "cam1", "allow_auto_from_mqtt": True}))
    ad2.send("mode/quality", "auto")
    assert rec2.calls == [("mode", "quality", "auto")]


def test_buttons_act_on_newest_only() -> None:
    _, ad, rec, _ = make()
    ad.send("approve_newest", "PRESS")
    ad.send("reject_newest", "PRESS")
    assert rec.calls == [("decide", 7, "approve", "ha button", "mqtt"), ("decide", 7, "reject", "ha button", "mqtt")]
    rec.calls.clear()
    rec.newest = None
    ad.send("approve_newest", "PRESS")
    assert rec.calls == []


def test_publish_decision_sensors_event_images() -> None:
    n, ad, _, _ = make()
    n.publish_decision(DECISION, "suggested")
    assert ad.last(f"{P}/state/last_decision") == b"pending: Glare at the gate"
    assert json.loads(ad.last(f"{P}/state/last_decision_attrs")) == {"preset": "gate", "problem": "glare", "status": "pending"}
    assert json.loads(ad.last(f"{P}/event/suggestion")) == {"event_type": "suggested", "id": 7}
    assert ad.last(f"{P}/image/before") == b"BEFORE" and ad.last(f"{P}/image/after") == b"AFTER"
    n.publish_decision({**DECISION, "status": "applied"}, "applied")
    assert json.loads(ad.last(f"{P}/event/suggestion"))["event_type"] == "applied"
    assert ad.topics().count(f"{P}/image/after") == 1  # images only for suggested
    n.publish_decision(DECISION, "weird")
    assert ad.topics().count(f"{P}/event/suggestion") == 2


def test_frames_not_published_when_disabled() -> None:
    n, ad, _, _ = make(parse_notify({"device": "cam1", "mqtt_frames": False}))
    n.publish_decision(DECISION, "suggested")
    n.publish_pending(1, DECISION)
    assert not [t for t in ad.topics() if "/image/" in t]


def test_publish_pending_and_status() -> None:
    n, ad, _, _ = make()
    n.publish_pending(2, DECISION)
    assert ad.last(f"{P}/state/pending") == b"2"
    assert json.loads(ad.last(f"{P}/state/pending_attrs")) == {"id": 7, "preset": "gate", "problem": "glare",
                                                              "change": "gain 20 -> 12"}
    n.publish_pending(0, None)
    assert json.loads(ad.last(f"{P}/state/pending_attrs")) == {}
    n.publish_status({"enabled": True, "capabilities": {"quality": {"mode": "suggest"}}, "pending": 3,
                      "models": {"calls_today": 5, "left": 95}})
    assert ad.last(f"{P}/state/enabled") == b"ON" and ad.last(f"{P}/state/mode/quality") == b"suggest"
    assert ad.last(f"{P}/state/pending") == b"3"
    assert ad.last(f"{P}/state/model_calls") == b"5" and ad.last(f"{P}/state/budget_left") == b"95"


def test_ntfy_request_shape(tmp_path: Path) -> None:
    tok = tmp_path / "tok"
    tok.write_text("s3cret\n")
    cfg = parse_notify({"device": "cam1", "ntfy": {"url": "https://ntfy.example/", "topic": "osprey",
                                                   "token_file": str(tok), "console_url": "https://c.example/assist"}})
    n, _, _, posts = make(cfg)
    n.publish_decision(DECISION, "suggested")
    url, body, h, method = posts[0]
    assert url == "https://ntfy.example/osprey" and method == "POST"
    assert body == b"Glare at the gate\nChange: gain 20 -> 12"
    assert h["Authorization"] == "Bearer s3cret" and h["Click"] == "https://c.example/assist"
    assert h["Priority"] == "default" and "gate" in h["Title"] and "Tags" in h
    assert not [k for k in h if k.lower() == "actions"]


def test_ntfy_attach_frames() -> None:
    cfg = parse_notify({"device": "cam1", "ntfy": {"url": "https://n.example", "topic": "t", "attach_frames": True}})
    n, _, _, posts = make(cfg)
    n.publish_decision(DECISION, "suggested")
    _, body, h, method = posts[0]
    assert method == "PUT" and body == b"AFTER" and h["Filename"] == "after.jpg"
    assert "Glare at the gate" in h["Message"] and "\n" not in h["Message"]
    assert "Authorization" not in h


def test_ntfy_failure_is_soft(tmp_path: Path) -> None:
    cfg = parse_notify({"device": "cam1", "ntfy": {"url": "https://n.example", "topic": "t",
                                                   "token_file": str(tmp_path / "missing")}})
    n, _, _, posts = make(cfg)
    n.publish_decision(DECISION, "suggested")  # unreadable token file
    assert posts == []

    def boom(*_a: Any) -> None:
        raise OSError("down")
    n.http_post = boom
    (tmp_path / "missing").write_text("x")
    n.publish_decision(DECISION, "applied")


def test_dead_broker_never_raises() -> None:
    n, ad, _, _ = make()
    ad.fail = True
    n.publish_decision(DECISION, "suggested")
    n.publish_pending(1, DECISION)
    n.publish_status({"enabled": True})
    n.stop()

    class Broken(FakeAdapter):
        def start(self) -> None:
            raise RuntimeError("needs paho")
    n2 = Notifier(parse_notify({}), Broken(), Rec().callbacks(), lambda _n: None)
    n2.start()
    n2.publish_decision(DECISION, "suggested")
    n2.stop()


def test_no_adapter_or_mqtt_off() -> None:
    n = Notifier(parse_notify({}), None, Rec().callbacks(), lambda _n: None)
    n.start()
    n.publish_status({})
    n.publish_decision(DECISION, "suggested")
    n.stop()
    ad = FakeAdapter()
    Notifier(parse_notify({"mqtt": False}), ad, Rec().callbacks(), lambda _n: None).start()
    assert not ad.started


def test_replay_on_reconnect() -> None:
    n, ad, _, _ = make()
    n.publish_pending(4, None)
    ad.pubs.clear()
    n._replay()
    assert f"{P}/availability" in ad.topics() and ad.last(f"{P}/state/pending") == b"4"


def test_parse_defaults() -> None:
    c = parse_notify(None)
    assert c.mqtt and c.mqtt_frames and c.ha_discovery and c.base_topic == "osprey" and c.device == "default"
    assert c.discovery_prefix == "homeassistant" and c.broker.port == 1883 and not c.allow_auto_from_mqtt
    assert c.ntfy.url is None and c.prefix == "osprey/default/assist"


@pytest.mark.parametrize("raw", [
    {"bogus": 1}, {"mqtt": "yes"}, {"device": "A/b"}, {"device": ""}, {"base_topic": "/x"}, {"base_topic": "a#"},
    {"broker": {"port": 0}}, {"broker": {"port": 70000}}, {"broker": {"port": True}}, {"broker": {"nope": 1}},
    {"broker": []}, {"broker": {"tls": 1}}, {"ntfy": {"url": "ftp://x", "topic": "t"}},
    {"ntfy": {"url": "https://x"}}, {"ntfy": {"attach_frames": "y"}}, {"allow_auto_from_mqtt": None},
])
def test_parse_rejects(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_notify(raw)
