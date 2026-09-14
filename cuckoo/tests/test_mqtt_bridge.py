from __future__ import annotations

import json

import pytest

from mqtt_bridge import MqttBridge, MqttConfig
from sentry_runtime import MovementGate


class FakeMqtt:
    def __init__(self) -> None:
        self.subscriptions: dict[str, object] = {}
        self.events: list[tuple[str, bytes]] = []

    def subscribe(self, topic: str, callback: object) -> None:
        self.subscriptions[topic] = callback

    def publish(self, topic: str, payload: bytes) -> None:
        self.events.append((topic, payload))


def bridge(fake: FakeMqtt, **kwargs: object) -> MqttBridge:
    return MqttBridge(MqttConfig.from_mapping({"enabled": True, "device": "front", "host": "broker"}), fake, MovementGate(), **kwargs)  # type: ignore[arg-type]


def test_disabled_bridge_does_not_subscribe() -> None:
    fake = FakeMqtt()
    MqttBridge(MqttConfig(), fake, MovementGate()).start()
    assert fake.subscriptions == {}


def test_enabled_config_requires_broker_and_keeps_password_out_of_status() -> None:
    with pytest.raises(ValueError):
        MqttConfig.from_mapping({"enabled": True})
    config = MqttConfig.from_mapping({"enabled": True, "host": "broker", "password": "secret", "tls": True})
    assert config.port == 8883
    assert config.as_dict()["password_configured"] is True
    assert "secret" not in repr(config.as_dict())


def test_commands_are_allow_listed_and_publish_structured_result() -> None:
    fake = FakeMqtt()
    calls: list[dict[str, object]] = []

    def step(data: dict[str, object]) -> dict[str, object]:
        calls.append(data)
        return {"ok": True}

    b = bridge(fake, step=step)
    b.start()
    topic = "osprey/front/command/ptz/step"
    assert topic in fake.subscriptions
    fake.subscriptions[topic](json.dumps({"axis": "pan", "direction": 1, "step": 4}).encode())  # type: ignore[operator]
    result = json.loads(fake.events[-1][1])
    assert result["accepted"] is True
    assert calls == [{"axis": "pan", "direction": 1, "step": 4}]


def test_malformed_or_unsafe_commands_are_rejected() -> None:
    fake = FakeMqtt()
    b = bridge(fake, zoom=lambda _data: True)
    b.start()
    topic = "osprey/front/command/ptz/zoom"
    fake.subscriptions[topic](b'{"percent":50,"extra":"move"}')  # type: ignore[operator]
    result = json.loads(fake.events[-1][1])
    assert result["accepted"] is False
    assert "percent" in result["error"]


def test_busy_shared_gate_rejects_without_invoking_handler() -> None:
    fake = FakeMqtt()
    calls: list[object] = []
    gate = MovementGate()
    b = MqttBridge(MqttConfig.from_mapping({"enabled": True, "host": "broker"}), fake, gate, home=lambda: calls.append(1))
    b.start()
    with gate.lease("autotracking"):
        fake.subscriptions["osprey/osprey/command/home"](b"{}")  # type: ignore[operator]
    result = json.loads(fake.events[-1][1])
    assert result["accepted"] is False
    assert calls == []
    assert result["error"] == "PTZ movement is reserved"


@pytest.mark.parametrize("raw", [{"device": "a/b"}, {"base_topic": "/osprey"}, {"enabled": 1}])
def test_config_rejects_bad_topic_config(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        MqttConfig.from_mapping(raw)
