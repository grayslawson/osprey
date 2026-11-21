from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable

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


def test_ptz_state_is_published_only_when_enabled() -> None:
    fake = FakeMqtt()
    bridge(fake).publish_state({"state": "UNKNOWN", "error": "motor state timeout"})
    topic, payload = fake.events[-1]
    assert topic.endswith("/state/ptz")
    assert json.loads(payload) == {"state": "UNKNOWN", "error": "motor state timeout"}

    quiet = FakeMqtt()
    MqttBridge(MqttConfig(), quiet, MovementGate()).publish_state({"state": "IDLE"})
    assert quiet.events == []


class _Broker:
    """A minimal MQTT 3.1.1 peer that records each connection's SUBSCRIBE topics."""

    def __init__(self) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.connections: list[list[str]] = []
        self.changed = threading.Condition()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with self.changed:
                self.connections.append([])
                topics = self.connections[-1]
            threading.Thread(target=self._serve, args=(conn, topics), daemon=True).start()

    @staticmethod
    def _read(conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _serve(self, conn: socket.socket, topics: list[str]) -> None:
        with conn:
            try:
                while True:
                    kind = self._read(conn, 1)[0] >> 4
                    length, shift = 0, 0
                    while True:
                        byte = self._read(conn, 1)[0]
                        length |= (byte & 0x7F) << shift
                        shift += 7
                        if not byte & 0x80:
                            break
                    body = self._read(conn, length)
                    if kind == 1:  # CONNECT
                        conn.sendall(b"\x20\x02\x00\x00")
                    elif kind == 8:  # SUBSCRIBE: packet id, then (len, topic, qos)*
                        size = int.from_bytes(body[2:4], "big")
                        with self.changed:
                            topics.append(body[4:4 + size].decode())
                            self.changed.notify_all()
                        conn.sendall(b"\x90\x03" + body[:2] + b"\x01")
                    elif kind == 12:  # PINGREQ
                        conn.sendall(b"\xd0\x00")
            except (ConnectionError, OSError):
                return

    def wait(self, predicate: Callable[[], bool], timeout: float = 10) -> bool:
        with self.changed:
            return self.changed.wait_for(predicate, timeout)


def test_paho_adapter_subscribes_once_connected_and_after_a_reconnect() -> None:
    pytest.importorskip("paho.mqtt.client")
    from mqtt_bridge import PahoMqttAdapter

    broker = _Broker()
    config = MqttConfig.from_mapping(
        {"enabled": True, "device": "front", "host": "127.0.0.1", "port": broker.port}
    )
    adapter = PahoMqttAdapter(config)
    bridge = MqttBridge(config, adapter, MovementGate(), home=lambda: None)
    bridge.start()
    try:
        topic = "osprey/front/command/home"
        assert broker.wait(lambda: bool(broker.connections) and topic in broker.connections[0])
        # A dropped connection starts a fresh session; the adapter must subscribe again.
        adapter._client._sock.close()
        assert broker.wait(lambda: len(broker.connections) >= 2 and topic in broker.connections[-1])
    finally:
        bridge.stop()
        broker.sock.close()
