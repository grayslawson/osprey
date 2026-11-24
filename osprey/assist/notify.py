"""Osprey Assist notifications: MQTT (with Home Assistant discovery) and ntfy.

Imports nothing from the controller.  paho-mqtt is the optional ``assist``
extra and is only imported by ``PahoAdapter.start``.  Everything here fails
soft: a dead broker or ntfy server is logged and never raises into the engine.
The notifier applies nothing itself; commands only call the injected callbacks.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

log = logging.getLogger("osprey.assist.notify")

MODES = ("off", "observe", "suggest", "act", "auto")
EVENT_TYPES = ("suggested", "applied", "reverted", "expired")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# ---------------------------------------------------------------- config

@dataclass(frozen=True)
class BrokerConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password_file: str | None = None
    tls: bool = False


@dataclass(frozen=True)
class NtfyConfig:
    url: str | None = None
    topic: str | None = None
    token_file: str | None = None
    attach_frames: bool = False
    console_url: str | None = None


@dataclass(frozen=True)
class NotifyConfig:
    mqtt: bool = True
    mqtt_frames: bool = True
    ha_discovery: bool = True
    discovery_prefix: str = "homeassistant"
    base_topic: str = "osprey"
    device: str = "default"
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    allow_auto_from_mqtt: bool = False
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)

    @property
    def prefix(self) -> str:
        return f"{self.base_topic}/{self.device}/assist"


def _section(raw: object, name: str, allowed: set[str]) -> Mapping[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"notify.{name} must be an object")
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown notify.{name} key(s): {', '.join(sorted(unknown))}")
    return raw


def _typed(value: object, kind: type, label: str, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ValueError(f"{label} must be {'a ' + kind.__name__}")
    return value


def _topic(value: object, label: str) -> str:
    s = _typed(value, str, label)
    if not s or s.strip("/") != s or "//" in s or any(c in s for c in "+#"):
        raise ValueError(f"{label} must be a non-empty topic prefix")
    return str(s)


def parse_notify(raw: dict[str, Any] | None) -> NotifyConfig:
    top = _section(raw, "", {"mqtt", "mqtt_frames", "ha_discovery", "discovery_prefix", "base_topic",
                             "device", "broker", "allow_auto_from_mqtt", "ntfy"})
    d = NotifyConfig()
    b = _section(top.get("broker"), "broker", {"host", "port", "username", "password_file", "tls"})
    n = _section(top.get("ntfy"), "ntfy", {"url", "topic", "token_file", "attach_frames", "console_url"})
    device = _typed(top.get("device", d.device), str, "notify.device")
    if not _SLUG.match(device):
        raise ValueError("notify.device must be a lowercase slug (a-z, 0-9, _ and -)")
    port = _typed(b.get("port", 1883), int, "notify.broker.port")
    if not 1 <= port <= 65535:
        raise ValueError("notify.broker.port must be from 1 to 65535")
    host = _typed(b.get("host", "127.0.0.1"), str, "notify.broker.host")
    if not host.strip():
        raise ValueError("notify.broker.host must not be empty")
    broker = BrokerConfig(
        host.strip(), port,
        _typed(b.get("username"), str, "notify.broker.username", True),
        _typed(b.get("password_file"), str, "notify.broker.password_file", True),
        _typed(b.get("tls", False), bool, "notify.broker.tls"))
    ntfy = NtfyConfig(
        _typed(n.get("url"), str, "notify.ntfy.url", True),
        _typed(n.get("topic"), str, "notify.ntfy.topic", True),
        _typed(n.get("token_file"), str, "notify.ntfy.token_file", True),
        _typed(n.get("attach_frames", False), bool, "notify.ntfy.attach_frames"),
        _typed(n.get("console_url"), str, "notify.ntfy.console_url", True))
    if ntfy.url is not None and not ntfy.url.startswith(("http://", "https://")):
        raise ValueError("notify.ntfy.url must be an http(s) URL")
    if ntfy.url is not None and not ntfy.topic:
        raise ValueError("notify.ntfy.topic is required when notify.ntfy.url is set")
    return NotifyConfig(
        _typed(top.get("mqtt", True), bool, "notify.mqtt"),
        _typed(top.get("mqtt_frames", True), bool, "notify.mqtt_frames"),
        _typed(top.get("ha_discovery", True), bool, "notify.ha_discovery"),
        _topic(top.get("discovery_prefix", d.discovery_prefix), "notify.discovery_prefix"),
        _topic(top.get("base_topic", d.base_topic), "notify.base_topic"),
        device, broker,
        _typed(top.get("allow_auto_from_mqtt", False), bool, "notify.allow_auto_from_mqtt"),
        ntfy)


# ---------------------------------------------------------------- adapters

class MqttAdapter(Protocol):
    def subscribe(self, topic: str, callback: Callable[[str, bytes, bool], None]) -> None: ...
    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None: ...
    def set_will(self, topic: str, payload: bytes) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


class PahoAdapter:
    """paho-mqtt adapter.  Paho handles reconnects; we resubscribe on every connect.

    ``on_connect`` (optional, no arguments) lets the notifier replay retained state.
    """

    def __init__(self, broker: BrokerConfig) -> None:
        self.broker = broker
        self.on_connect: Callable[[], None] | None = None
        self._client: Any = None
        self._subs: dict[str, Callable[[str, bytes, bool], None]] = {}
        self._will: tuple[str, bytes] | None = None

    def set_will(self, topic: str, payload: bytes) -> None:
        self._will = (topic, payload)

    def subscribe(self, topic: str, callback: Callable[[str, bytes, bool], None]) -> None:
        self._subs[topic] = callback
        if self._client is not None:
            self._client.subscribe(topic, qos=1)

    def start(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("Assist MQTT needs paho-mqtt: install the 'assist' extra "
                               "(pip install 'osprey-ptz[assist]')") from exc
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)  # type: ignore[attr-defined]
        if self.broker.username is not None:
            pw = None
            if self.broker.password_file:
                pw = Path(self.broker.password_file).read_text(encoding="utf-8").strip()
            client.username_pw_set(self.broker.username, pw)
        if self.broker.tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        if self._will:
            client.will_set(self._will[0], self._will[1], qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.connect_async(self.broker.host, self.broker.port, keepalive=60)
        client.loop_start()
        self._client = client

    def _on_connect(self, client: Any, _u: object, _f: object, reason: Any, _p: object = None) -> None:
        if getattr(reason, "is_failure", False):
            log.warning("assist MQTT broker refused the connection: %s", reason)
            return
        for topic in list(self._subs):
            client.subscribe(topic, qos=1)
        if self.on_connect:
            try:
                self.on_connect()
            except Exception:
                log.exception("assist MQTT on_connect replay failed")

    def _on_message(self, _c: object, _u: object, msg: Any) -> None:
        cb = self._subs.get(msg.topic)
        if cb:
            cb(msg.topic, bytes(msg.payload), bool(msg.retain))

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if self._client is None:
            raise RuntimeError("MQTT adapter is not started")
        self._client.publish(topic, payload, qos=1, retain=retain)

    def stop(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()
            self._client = None


# ---------------------------------------------------------------- notifier

@dataclass
class Callbacks:
    decide: Callable[[int, str, str, str], None]
    set_enabled: Callable[[bool], None]
    set_mode: Callable[[str, str], None]
    check_now: Callable[[], None]
    newest_pending: Callable[[], dict[str, Any] | None]


# post(url, body, headers, method) -> None; raises on failure.
HttpPost = Callable[[str, bytes, dict[str, str], str], None]


def _urllib_post(url: str, body: bytes, headers: dict[str, str], method: str) -> None:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - operator-configured URL
        resp.read()


def _hdr(text: str) -> str:
    """HTTP headers are latin-1 and single-line."""
    return " ".join(text.split()).encode("latin-1", "replace").decode("latin-1")


class Notifier:
    def __init__(self, cfg: NotifyConfig, adapter: MqttAdapter | None, callbacks: Callbacks,
                 frames: Callable[[str], bytes | None], http_post: HttpPost = _urllib_post,
                 clock: Callable[[], float] | None = None) -> None:
        self.cfg, self.adapter, self.cb, self.frames = cfg, adapter, callbacks, frames
        self.http_post, self.clock = http_post, clock or time.time
        self.p = cfg.prefix
        self._lock = threading.Lock()
        self._retained: dict[str, bytes] = {}
        self._ident = f"osprey_{cfg.device}_assist"
        self._selects: list[tuple[str, str]] = [("quality", "Quality mode")]  # (capability, name)
        self._active = False

    # -- lifecycle
    def start(self) -> None:
        a = self.adapter
        if a is None or not self.cfg.mqtt:
            return
        try:
            a.set_will(f"{self.p}/availability", b"offline")
            if hasattr(a, "on_connect"):
                a.on_connect = self._replay
            a.start()
            self._active = True
            self._subscribe_all()
            if self.cfg.ha_discovery:
                self._discovery()
            self._pub(f"{self.p}/availability", b"online", retain=True)
        except Exception as exc:
            log.warning("assist MQTT start failed (%s); continuing without MQTT", type(exc).__name__)

    def stop(self) -> None:
        if self.adapter is None or not self._active:
            return
        self._pub(f"{self.p}/availability", b"offline", retain=True)
        try:
            self.adapter.stop()
        except Exception as exc:
            log.warning("assist MQTT stop failed (%s)", type(exc).__name__)
        self._active = False

    def _replay(self) -> None:
        with self._lock:
            items = list(self._retained.items())
        for topic, payload in items:
            self._send(topic, payload, True)

    # -- publishing helpers
    def _send(self, topic: str, payload: bytes, retain: bool) -> None:
        if self.adapter is None or not self._active:
            return
        try:
            self.adapter.publish(topic, payload, retain)
        except Exception as exc:
            log.warning("assist MQTT publish failed on %s (%s)", topic, type(exc).__name__)

    def _pub(self, topic: str, payload: bytes | str | dict[str, Any], retain: bool = False) -> None:
        if self.adapter is None or not self.cfg.mqtt:
            return
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":"))
        data = payload.encode() if isinstance(payload, str) else payload
        if retain:
            with self._lock:
                self._retained[topic] = data
        self._send(topic, data, retain)

    # -- discovery
    def _device(self) -> dict[str, Any]:
        return {"identifiers": [self._ident], "name": f"Osprey Assist ({self.cfg.device})",
                "manufacturer": "Osprey", "model": "Assist"}

    def _entity(self, component: str, key: str, name: str, **extra: Any) -> tuple[str, dict[str, Any]]:
        conf: dict[str, Any] = {
            "name": name, "unique_id": f"{self._ident}_{key}", "object_id": f"{self._ident}_{key}",
            "device": self._device(), "availability_topic": f"{self.p}/availability",
            "payload_available": "online", "payload_not_available": "offline", **extra}
        return f"{self.cfg.discovery_prefix}/{component}/{self._ident}/{key}/config", conf

    def discovery_configs(self) -> list[tuple[str, dict[str, Any]]]:
        p = self.p
        out = [
            self._entity("switch", "enabled", "Assist enabled", state_topic=f"{p}/state/enabled",
                         command_topic=f"{p}/command/enabled", payload_on="ON", payload_off="OFF"),
            self._entity("sensor", "pending", "Pending approvals", state_topic=f"{p}/state/pending",
                         json_attributes_topic=f"{p}/state/pending_attrs"),
            self._entity("sensor", "last_decision", "Last decision", state_topic=f"{p}/state/last_decision",
                         json_attributes_topic=f"{p}/state/last_decision_attrs"),
            self._entity("image", "suggestion_before", "Suggestion before",
                         image_topic=f"{p}/image/before", content_type="image/jpeg"),
            self._entity("image", "suggestion_after", "Suggestion after",
                         image_topic=f"{p}/image/after", content_type="image/jpeg"),
            self._entity("button", "approve_newest", "Approve newest", command_topic=f"{p}/command/approve_newest"),
            self._entity("button", "reject_newest", "Reject newest", command_topic=f"{p}/command/reject_newest"),
            self._entity("event", "suggestion", "Suggestion", state_topic=f"{p}/event/suggestion",
                         event_types=list(EVENT_TYPES)),
            self._entity("sensor", "model_calls", "Model calls today", state_topic=f"{p}/state/model_calls",
                         state_class="total_increasing"),
            self._entity("sensor", "budget_left", "Budget left", state_topic=f"{p}/state/budget_left"),
        ]
        for cap, name in self._selects:
            out.append(self._entity("select", f"{cap}_mode", name, state_topic=f"{p}/state/mode/{cap}",
                                    command_topic=f"{p}/command/mode/{cap}", options=list(MODES)))
        if not self.cfg.mqtt_frames:
            out = [e for e in out if "image_topic" not in e[1]]
        return out

    def _discovery(self) -> None:
        for topic, conf in self.discovery_configs():
            self._pub(topic, conf, retain=True)

    # -- commands
    def _subscribe_all(self) -> None:
        assert self.adapter is not None
        topics = ["decision", "enabled", "approve_newest", "reject_newest", "check"]
        topics += [f"mode/{cap}" for cap, _ in self._selects]
        for t in topics:
            self.adapter.subscribe(f"{self.p}/command/{t}", self._on_message)

    def _on_message(self, topic: str, payload: bytes, retained: bool) -> None:
        if retained:
            log.info("ignoring retained assist command on %s", topic)
            return
        action = topic.removeprefix(f"{self.p}/command/")
        try:
            self._dispatch(action, payload.decode("utf-8", "replace").strip())
        except Exception as exc:
            log.warning("assist MQTT command %s rejected: %s", action, str(exc) or type(exc).__name__)

    def _dispatch(self, action: str, text: str) -> None:
        cb = self.cb
        if action == "decision":
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("payload must be a JSON object")
            did, verdict, reason = data.get("id"), data.get("verdict"), data.get("reason", "")
            if isinstance(did, bool) or not isinstance(did, int):
                raise ValueError("id must be an integer")
            if verdict not in ("approve", "reject") or not isinstance(reason, str):
                raise ValueError("verdict must be approve or reject; reason a string")
            cb.decide(did, verdict, reason[:500], "mqtt")
        elif action in ("approve_newest", "reject_newest"):
            newest = cb.newest_pending()
            if newest is None:
                log.info("assist %s: nothing pending", action)
                return
            cb.decide(int(newest["id"]), action.split("_")[0], "ha button", "mqtt")
        elif action == "enabled":
            if text.upper() not in ("ON", "OFF"):
                raise ValueError("enabled must be ON or OFF")
            cb.set_enabled(text.upper() == "ON")
        elif action == "check":
            cb.check_now()
        elif action.startswith("mode/"):
            cap = action[5:]
            if text not in MODES:
                raise ValueError(f"unknown mode {text!r}")
            if text == "auto" and not self.cfg.allow_auto_from_mqtt:
                raise ValueError("auto mode cannot be set over MQTT (allow_auto_from_mqtt is false)")
            cb.set_mode(cap, text)

    # -- state
    def publish_status(self, status: dict[str, Any]) -> None:
        p = self.p
        self._pub(f"{p}/state/enabled", "ON" if status.get("enabled") else "OFF", retain=True)
        for cap, info in (status.get("capabilities") or {}).items():
            self._pub(f"{p}/state/mode/{cap}", str(info.get("mode", "off")), retain=True)
        self._pub(f"{p}/state/pending", str(int(status.get("pending", 0))), retain=True)
        models = status.get("models") or {}
        self._pub(f"{p}/state/model_calls", str(models.get("calls_today", 0)), retain=True)
        self._pub(f"{p}/state/budget_left", str(models.get("left", "")), retain=True)

    def publish_pending(self, count: int, newest: dict[str, Any] | None) -> None:
        self._pub(f"{self.p}/state/pending", str(count), retain=True)
        attrs = {k: newest.get(k) for k in ("id", "preset", "problem", "change")} if newest else {}
        self._pub(f"{self.p}/state/pending_attrs", attrs, retain=True)
        if newest:
            self._publish_frames(newest)

    def _publish_frames(self, d: dict[str, Any]) -> None:
        if not self.cfg.mqtt_frames:
            return
        for key, slot in (("before_frame", "before"), ("after_frame", "after")):
            name = d.get(key)
            if not name:
                continue
            try:
                jpeg = self.frames(name)
            except Exception as exc:
                log.warning("assist frame %s unreadable (%s)", name, type(exc).__name__)
                continue
            if jpeg:
                self._pub(f"{self.p}/image/{slot}", jpeg, retain=True)

    def publish_decision(self, decision: dict[str, Any], event: str) -> None:
        p = self.p
        summary = str(decision.get("summary", ""))
        status = decision.get("status")
        self._pub(f"{p}/state/last_decision", f"{status}: {summary}"[:255], retain=True)
        self._pub(f"{p}/state/last_decision_attrs",
                  {k: decision.get(k) for k in ("preset", "problem", "status")}, retain=True)
        if event in EVENT_TYPES:
            self._pub(f"{p}/event/suggestion", {"event_type": event, "id": decision.get("id")})
            if event == "suggested":
                self._publish_frames(decision)
        else:
            log.warning("assist: unknown event type %r not published", event)
        self._ntfy(decision, event)

    # -- ntfy
    def _ntfy(self, d: dict[str, Any], event: str) -> None:
        n = self.cfg.ntfy
        if not n.url or not n.topic:
            return
        try:
            text = f"{d.get('summary', '')}\nChange: {d.get('change', '')}"
            if d.get("warning"):
                text += f"\nWarning: {d['warning']}"
            headers = {"Title": _hdr(f"Osprey Assist {event}: {d.get('preset', '')} - {d.get('problem', '')}"),
                       "Priority": "default", "Tags": "camera"}
            if n.console_url:
                headers["Click"] = _hdr(n.console_url)
            if n.token_file:
                headers["Authorization"] = "Bearer " + Path(n.token_file).read_text(encoding="utf-8").strip()
            url = f"{n.url.rstrip('/')}/{n.topic}"
            body, method = text.encode(), "POST"
            jpeg = self.frames(str(d["after_frame"])) if n.attach_frames and d.get("after_frame") else None
            if jpeg:
                body, method = jpeg, "PUT"
                headers["Message"] = _hdr(text)
                headers["Filename"] = "after.jpg"
            self.http_post(url, body, headers, method)
        except Exception as exc:
            log.warning("assist ntfy send failed (%s)", type(exc).__name__)
