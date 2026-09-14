"""Optional MQTT command/event bridge for Osprey.

This module has no broker dependency.  An application supplies an adapter with
``subscribe(topic, callback)`` and ``publish(topic, payload)`` methods (the
callback receives bytes).  The default contract is::

    osprey/<device>/command/ptz/step   {"axis":"pan","direction":1,"step":4}
    osprey/<device>/command/ptz/zoom   {"percent":50}
    osprey/<device>/command/preset/goto {"name":"gate"}
    osprey/<device>/command/home       {}
    osprey/<device>/event/control      structured result/error JSON

Commands are deliberately allow-listed and always execute under a shared
MovementGate.  MQTT must never become an unauthenticated arbitrary movement
API; broker ACLs and authentication remain the embedding application's job.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from sentry_runtime import MovementGate

log = logging.getLogger("cuckoo.mqtt")


class MqttAdapter(Protocol):
    def subscribe(self, topic: str, callback: Callable[[bytes], None]) -> None: ...
    def publish(self, topic: str, payload: bytes) -> None: ...


@dataclass(frozen=True)
class MqttConfig:
    enabled: bool = False
    device: str = "osprey"
    base_topic: str = "osprey"
    host: str | None = None
    port: int = 1883
    tls: bool = False
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> "MqttConfig":
        value = raw or {}
        enabled = value.get("enabled", False)
        device = value.get("device", "osprey")
        base = value.get("base_topic", "osprey")
        host = value.get("host")
        port = value.get("port", 8883 if value.get("tls", False) else 1883)
        tls = value.get("tls", False)
        username = value.get("username")
        password = value.get("password")
        if not isinstance(enabled, bool):
            raise ValueError("mqtt.enabled must be boolean")
        if not isinstance(device, str) or not device.strip() or "/" in device:
            raise ValueError("mqtt.device must be a non-empty topic segment")
        if not isinstance(base, str) or not base.strip() or base.strip("/") != base or "//" in base:
            raise ValueError("mqtt.base_topic must be a non-empty topic prefix")
        if enabled and (not isinstance(host, str) or not host.strip()):
            raise ValueError("mqtt.host is required when MQTT is enabled")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("mqtt.port must be an integer from 1 to 65535")
        if not isinstance(tls, bool):
            raise ValueError("mqtt.tls must be boolean")
        if username is not None and not isinstance(username, str):
            raise ValueError("mqtt.username must be a string")
        if password is not None and not isinstance(password, str):
            raise ValueError("mqtt.password must be a string")
        return cls(enabled, device.strip(), base, host.strip() if isinstance(host, str) else None, port, tls, username, password)

    @property
    def prefix(self) -> str:
        return f"{self.base_topic}/{self.device}"

    def as_dict(self) -> dict[str, object]:
        return {"enabled": self.enabled, "device": self.device, "base_topic": self.base_topic,
                "host": self.host, "port": self.port, "tls": self.tls,
                "username": self.username, "password_configured": self.password is not None}


class PahoMqttAdapter:
    """Small optional Paho adapter; import and broker connection are lazy.

    Paho's loop performs reconnects with bounded exponential backoff.  Retained
    command messages are ignored so a broker cannot replay an old movement after
    a restart.  Configure broker ACLs/TLS and credentials in deployment secrets.
    """

    def __init__(self, config: MqttConfig) -> None:
        if not config.enabled or not config.host:
            raise ValueError("enabled MQTT config with host required")
        self.config = config
        self._client: Any = None

    def start(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("MQTT is enabled but optional dependency paho-mqtt is not installed") from exc
        host = self.config.host
        if host is None:  # validated at construction; keeps this path type-safe.
            raise RuntimeError("MQTT is enabled but broker host is missing")
        client = mqtt.Client()
        if self.config.username is not None:
            client.username_pw_set(self.config.username, self.config.password)
        if self.config.tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.connect_async(host, self.config.port, keepalive=60)
        client.loop_start()
        self._client = client

    def subscribe(self, topic: str, callback: Callable[[bytes], None]) -> None:
        client = self._require_client()
        def receive(_client: object, _userdata: object, message: object) -> None:
            if getattr(message, "retain", False):
                log.info("ignoring retained MQTT command on %s", topic)
                return
            callback(bytes(getattr(message, "payload", b"")))
        client.message_callback_add(topic, receive)
        client.subscribe(topic, qos=1)

    def publish(self, topic: str, payload: bytes) -> None:
        self._require_client().publish(topic, payload, qos=1, retain=False)

    def stop(self) -> None:
        client = self._client
        if client is not None:
            client.disconnect()
            client.loop_stop()
            self._client = None

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("MQTT adapter is not started")
        return self._client


class MqttBridge:
    """Validate, arbitrate, and dispatch the small Osprey MQTT command set."""

    def __init__(
        self,
        config: MqttConfig,
        adapter: MqttAdapter,
        gate: MovementGate,
        *,
        step: Callable[[dict[str, object]], object] | None = None,
        zoom: Callable[[dict[str, object]], object] | None = None,
        goto_preset: Callable[[str], object] | None = None,
        home: Callable[[], object] | None = None,
    ) -> None:
        self.config, self.adapter, self.gate = config, adapter, gate
        self._handlers: dict[str, Callable[[dict[str, object]], object]] = {}
        if step:
            self._handlers["ptz/step"] = step
        if zoom:
            self._handlers["ptz/zoom"] = zoom
        if goto_preset:
            self._handlers["preset/goto"] = lambda data: goto_preset(str(data["name"]))
        if home:
            self._handlers["home"] = lambda _data: home()

    def start(self) -> None:
        if not self.config.enabled:
            return
        start = getattr(self.adapter, "start", None)
        if callable(start):
            start()
        for action in self._handlers:
            self.adapter.subscribe(f"{self.config.prefix}/command/{action}", self._receive(action))

    def stop(self) -> None:
        stop = getattr(self.adapter, "stop", None)
        if callable(stop):
            stop()

    def _receive(self, action: str) -> Callable[[bytes], None]:
        def receive(raw: bytes) -> None:
            result: dict[str, object] = {"action": action, "accepted": False, "at": time.time()}
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("payload must be a JSON object")
                self._validate(action, data)
                with self.gate.lease("mqtt"):
                    result["result"] = self._handlers[action](data)
                result["accepted"] = True
            except (ValueError, TypeError, json.JSONDecodeError, BlockingIOError) as exc:
                result["error"] = str(exc) or type(exc).__name__
            except Exception as exc:  # adapter boundary must not kill MQTT loop
                result["error"] = f"handler error: {type(exc).__name__}"
            self._emit(result)
        return receive

    @staticmethod
    def _validate(action: str, data: dict[str, object]) -> None:
        if action == "ptz/step":
            if set(data) - {"axis", "direction", "step"} or not isinstance(data.get("axis"), str):
                raise ValueError("step requires axis, direction, and optional step")
            if data["axis"] not in ("pan", "tilt", "zoom") or data.get("direction") not in (-1, 1):
                raise ValueError("invalid step axis or direction")
            step = data.get("step")
            if "step" in data:
                if not isinstance(step, int) or isinstance(step, bool):
                    raise ValueError("step must be an integer from 1 to 20")
                if not 1 <= step <= 20:
                    raise ValueError("step must be an integer from 1 to 20")
        elif action == "ptz/zoom":
            percent = data.get("percent")
            if set(data) != {"percent"} or not isinstance(percent, int) or isinstance(percent, bool):
                raise ValueError("percent must be an integer from 0 to 100")
            if not 0 <= percent <= 100:
                raise ValueError("percent must be an integer from 0 to 100")
        elif action == "preset/goto":
            name = data.get("name")
            if set(data) != {"name"} or not isinstance(name, str) or not name.strip() or len(name) > 64:
                raise ValueError("name must be a non-empty string up to 64 characters")
        elif action == "home":
            if data:
                raise ValueError("home takes an empty object")

    def _emit(self, result: dict[str, object]) -> None:
        payload = json.dumps(result, separators=(",", ":")).encode()
        self.adapter.publish(f"{self.config.prefix}/event/control", payload)
