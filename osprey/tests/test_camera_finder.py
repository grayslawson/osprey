"""UniFi discovery for the setup wizard: fixture packets and a fake device. No LAN."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

import camera_finder
from unifiwire import discovery as wire


def tlv(kind: int, value: bytes) -> bytes:
    return bytes([kind]) + len(value).to_bytes(2, "big") + value


def reply(*fields: bytes) -> bytes:
    body = b"".join(fields)
    return bytes([1, 0]) + len(body).to_bytes(2, "big") + body


MAC = bytes.fromhex("f4e2c6a1b2c3")
FIRMWARE = b"UVC.SAV530q.v5.5.13.67.c2f8b94.260907.1843"


def g5_reply(managed_flag: int | None = 0, firmware: bytes = FIRMWARE,
             platform: bytes = b"UVC G5 PTZ", ip: bytes = bytes([192, 0, 2, 20])) -> bytes:
    """The eleven fields a real G5 PTZ sends, in its order (pyunifiwire/SPEC.md 1a)."""
    fields = [
        tlv(0x02, MAC + ip), tlv(0x01, MAC), tlv(0x0A, (86400).to_bytes(4, "big")),
        tlv(0x0B, b"G5 PTZ"), tlv(0x0C, platform),
    ]
    if managed_flag is not None:
        fields.append(tlv(0x17, managed_flag.to_bytes(4, "big")))
    fields += [
        tlv(0x03, firmware), tlv(0x10, bytes([0x9B, 0xA5])),
        tlv(0x20, b"3f2a8d4e-0c1b-4f5a-9e7d-6b8c9a0d1e2f"), tlv(0x2B, bytes(range(16))), tlv(0x2C, b"\x03"),
    ]
    return reply(*fields)


def test_a_g5_reply_becomes_a_pick_list_entry() -> None:
    camera = camera_finder.describe(g5_reply(), "192.0.2.20")
    assert camera == {
        "mac": "F4:E2:C6:A1:B2:C3",
        "ip": "192.0.2.20",
        "model": "UVC G5 PTZ",
        "hostname": "G5 PTZ",
        "firmware": FIRMWARE.decode(),
        "firmware_release": "5.5.13",
        "firmware_status": "tested",
        "firmware_message": "Firmware UVC.SAV530q.v5.5.13.67.c2f8b94.260907.1843 is a tested version.",
        "managed": True,
        "ptz": True,
    }


@pytest.mark.parametrize("flag, managed", [(0, True), (1, False), (None, None)])
def test_the_management_flag_says_whether_a_controller_holds_the_camera(
    flag: int | None, managed: bool | None,
) -> None:
    camera = camera_finder.describe(g5_reply(managed_flag=flag), "192.0.2.20")
    assert camera is not None and camera["managed"] is managed


def test_untested_firmware_is_flagged() -> None:
    camera = camera_finder.describe(g5_reply(firmware=b"UVC.SAV530q.v5.9.1.67.abc.270101.0000"), "192.0.2.20")
    assert camera is not None
    assert camera["firmware_status"] == "untested" and "has not been tested" in str(camera["firmware_message"])


def test_the_library_encoder_and_the_reader_agree() -> None:
    sent = wire.announce("f4:e2:c6:a1:b2:c3", "192.0.2.21", "G5 PTZ", "UVC G5 PTZ", "5.3.95", 0xA59B)
    camera = camera_finder.describe(sent.to_bytes(), "192.0.2.21")
    assert camera is not None and camera["ip"] == "192.0.2.21" and camera["firmware_status"] == "tested"


@pytest.mark.parametrize("datagram", [
    wire.probe(),                                     # our own probe, looped back
    reply(tlv(0x02, MAC + bytes(4)), tlv(0x0C, b"U7 Pro")),  # an access point
    b"\x01\x00",                                      # shorter than a header
    reply(tlv(0x0C, b"UVC G5 PTZ")),                  # a camera with no MAC
    g5_reply() + bytes(camera_finder.MAX_DATAGRAM),   # oversize
])
def test_anything_that_is_not_a_camera_reply_is_ignored(datagram: bytes) -> None:
    assert camera_finder.describe(datagram, "192.0.2.30") is None


def test_a_truncated_reply_keeps_what_parsed() -> None:
    cut = g5_reply()[:-40]
    camera = camera_finder.describe(cut, "192.0.2.20")
    assert camera is not None and camera["mac"] == "F4:E2:C6:A1:B2:C3"


def test_control_characters_never_reach_the_browser() -> None:
    camera = camera_finder.describe(g5_reply(platform=b"UVC G5\x1b[31m PTZ\n" + b"x" * 300), "192.0.2.20")
    assert camera is not None
    model = str(camera["model"])
    assert "\x1b" not in model and "\n" not in model and len(model) <= camera_finder.MAX_TEXT


@pytest.fixture
def fake_camera() -> Iterator[int]:
    """A device on loopback that answers each probe the way a camera does."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.1)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                datagram, sender = sock.recvfrom(64)
            except (TimeoutError, OSError):
                continue
            if wire.is_probe(datagram):
                sock.sendto(g5_reply(managed_flag=1, ip=bytes([127, 0, 0, 1])), sender)
                sock.sendto(b"garbage", sender)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield int(sock.getsockname()[1])
    stop.set()
    thread.join()
    sock.close()


def test_a_scan_lists_each_camera_once(fake_camera: int) -> None:
    found = camera_finder.scan(seconds=0.6, targets=[("127.0.0.1", fake_camera)])
    assert [c["mac"] for c in found] == ["F4:E2:C6:A1:B2:C3"]
    assert found[0]["managed"] is False


def test_a_typed_address_is_probed_directly(fake_camera: int) -> None:
    camera = camera_finder.probe("127.0.0.1", seconds=1.0, port=fake_camera)
    assert camera is not None and camera["ip"] == "127.0.0.1"


def test_a_silent_address_finds_nothing() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as quiet:
        quiet.bind(("127.0.0.1", 0))
        assert camera_finder.probe("127.0.0.1", seconds=0.3, port=quiet.getsockname()[1]) is None


def test_a_typed_address_must_be_ipv4() -> None:
    with pytest.raises(ValueError):
        camera_finder.probe("not-an-ip")


ROUTES = """Iface	Destination	Gateway 	Flags	RefCnt	Use	Metric	Mask		MTU	Window	IRTT
eth0	00000000	014D590A	0003	0	0	100	00000000	0	0	0
eth0	004D590A	00000000	0001	0	0	0	00FFFFFF	0	0	0
eth1	0000A8C0	00000000	0001	0	0	0	0000FFFF	0	0	0
eth1	FF00640A	00000000	0005	0	0	261	FFFFFFFF	0	0	0
lo	0000007F	00000000	0001	0	0	0	000000FF	0	0	0
bad	zz	00000000	0001	0	0	0	00FFFFFF	0	0	0
"""


def test_each_connected_subnet_gets_its_directed_broadcast() -> None:
    assert camera_finder.subnet_broadcasts(ROUTES) == ["10.89.77.255", "192.168.255.255"]
