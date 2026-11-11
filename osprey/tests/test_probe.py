"""``osprey probe``: read-only model data gathering with redaction."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import probe
from model import Camera
from unifiwire import discovery

CAMERA_MAC = "aa:bb:cc:dd:ee:ff"
HELLO = {
    "fwVersion": "UVC.SAV530q.v5.3.95.67.148b9a3.260612.0645",
    "lensmodel": "UVC G7 PTZ",
    "name": "Back yard",
    "mac": "AABBCCDDEEFF",
    "ip": "192.0.2.20",
    "serialNumber": "F4E2C6000001",
    "guid": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
    "token": "adoption-secret",
    "features": {
        "pan": {"steps": {"min": 500, "max": 35500}},
        "tilt": {"steps": {"min": 8000, "max": 18000}},
        "zoom": {"steps": {"min": 0, "max": 730}},
        "mic": 1,
    },
}


def _dump(tmp_path: Path) -> Path:
    lines = [
        {"direction": "in", "functionName": "ubnt_avclient_hello", "payload": HELLO},
        {"direction": "out", "functionName": "ubnt_avclient_hello", "payload": {"controllerName": "x"}},
        *[
            {"direction": "in", "functionName": "EventMotorState",
             "payload": {"state": {"activity": 0, "position": {"pan": n}}}}
            for n in range(5)
        ],
        "not json",
    ]
    path = tmp_path / "messages.jsonl"
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
    return path


def test_dump_bundle_keeps_model_facts_and_redacts_identity(tmp_path: Path) -> None:
    bundle = probe.build(None, _dump(tmp_path), timeout=0.1)
    text = json.dumps(bundle)
    for secret in ("Back yard", "AABBCCDDEEFF", "192.0.2.20", "F4E2C6000001",
                   "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "adoption-secret"):
        assert secret not in text
    summary = bundle["summary"]
    assert summary["lensmodel"] == "UVC G7 PTZ"
    assert summary["firmware"] == HELLO["fwVersion"]  # dotted version is not an IP
    assert summary["ptz_steps"]["pan"] == {"min": 500, "max": 35500}
    assert summary["support"] == "experimental"
    dump = bundle["dump"]
    assert dump["counts"]["EventMotorState"] == 5
    assert len(dump["samples"]["EventMotorState"]) == probe.SAMPLES_PER_FUNCTION
    assert "controllerName" not in text  # only what the camera sent


def test_redact_handles_free_text_and_keeps_booleans() -> None:
    out = probe.redact({"note": "from 192.0.2.7 mac aa:bb:cc:dd:ee:ff", "hasMac": True,
                        "list": ["10.0.0.1"], "streamName": "video1"})
    assert out == {"note": "from <ipv4> mac <mac>", "hasMac": True, "list": ["<ipv4>"],
                   "streamName": "video1"}


def test_discovery_reads_the_unicast_answer() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    answer = discovery.Announcement(fields=[
        discovery.Field(discovery.PLATFORM, b"UVC G5 PTZ"),
        discovery.Field(discovery.FWVERSION, b"UVC.SAV530q.v5.3.95"),
        discovery.Field(discovery.SYSTEM_ID, bytes([0x9B, 0xA5])),
        discovery.Field(discovery.HWADDR, discovery.mac_bytes(CAMERA_MAC)),
    ]).to_bytes()

    def reply() -> None:
        data, peer = server.recvfrom(64)
        assert discovery.is_probe(data)
        server.sendto(answer, peer)

    thread = threading.Thread(target=reply)
    thread.start()
    original = probe.DISCOVERY_PORT
    probe.DISCOVERY_PORT = port  # type: ignore[misc]
    try:
        bundle = probe.build("127.0.0.1", None, timeout=2)
    finally:
        probe.DISCOVERY_PORT = original  # type: ignore[misc]
        thread.join(timeout=2)
        server.close()
    assert bundle["discovery"]["answered"] is True
    assert bundle["summary"]["system_id"] == "0xa59b"
    assert bundle["summary"]["support"] == "supported"
    assert CAMERA_MAC not in json.dumps(bundle)


def test_silent_camera_is_reported_not_raised() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    original = probe.DISCOVERY_PORT
    probe.DISCOVERY_PORT = sock.getsockname()[1]  # type: ignore[misc]
    try:
        assert probe.discover("127.0.0.1", timeout=0.2)["answered"] is False
    finally:
        probe.DISCOVERY_PORT = original  # type: ignore[misc]
        sock.close()


def test_cli_writes_a_file(tmp_path: Path) -> None:
    out = tmp_path / "bundle.json"
    assert probe.main(["--dump", str(_dump(tmp_path)), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["bundle_version"] == probe.BUNDLE_VERSION


def test_only_measured_models_are_supported() -> None:
    assert Camera(mac="AABBCCDDEEFF", model="UVC G5 PTZ").support == "supported"
    assert Camera(mac="AABBCCDDEEFF", model="UVC G7 PTZ").support == "experimental"
