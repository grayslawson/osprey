"""Device discovery on UDP 10001.

The bytes in this file are a real exchange, with the device's own identifiers
replaced — the shapes are what was measured, the values are invented.
"""

from __future__ import annotations

import pytest

from unifiwire import discovery

# A camera's reply, field for field and in the order one was captured — with the
# device's own identifiers replaced by invented ones.
CAPTURED: list[tuple[int, bytes]] = [
    (discovery.IPINFO, bytes.fromhex("aabbccddeeff") + bytes([192, 168, 2, 50])),
    (discovery.HWADDR, bytes.fromhex("aabbccddeeff")),
    (discovery.UPTIME, (34306).to_bytes(4, "big")),
    (discovery.HOSTNAME, b"G5 PTX"),
    (discovery.PLATFORM, b"UVC G5 PTZ"),
    (discovery.MGMT_IS_DEFAULT, bytes(4)),
    (discovery.FWVERSION, b"UVC.SAV530q.v5.3.95.67"),
    (discovery.SYSTEM_ID, bytes.fromhex("9ba5")),      # little endian: 0xa59b
    (discovery.DEFAULT_CREDENTIALS, bytes([3])),
]


def a_reply() -> bytes:
    body = b"".join(discovery.encode_field(kind, value) for kind, value in CAPTURED)
    return bytes([1, 0]) + len(body).to_bytes(2, "big") + body


def test_the_probe_is_four_bytes() -> None:
    assert discovery.probe() == bytes([1, 0, 0, 0])
    assert discovery.is_probe(discovery.probe())
    assert not discovery.is_probe(a_reply())


def test_a_reply_is_read_field_by_field() -> None:
    announcement = discovery.parse(a_reply())
    assert announcement.version == 1 and announcement.command == 0
    assert [f.name for f in announcement.fields][:4] == [
        "ipinfo", "hwaddr", "uptime", "hostname"
    ]


def test_the_address_field_carries_mac_and_ip_together() -> None:
    announcement = discovery.parse(a_reply())
    assert announcement.mac == "aa:bb:cc:dd:ee:ff"
    assert announcement.ip == "192.168.2.50"


def test_platform_and_firmware_are_text() -> None:
    announcement = discovery.parse(a_reply())
    assert announcement.platform == "UVC G5 PTZ"
    assert announcement.firmware == "UVC.SAV530q.v5.3.95.67"


def test_system_id_is_little_endian_here_and_big_endian_in_the_header() -> None:
    """`9b a5` on the wire is model 0xa59b — the same value the WebSocket header
    carries the other way round. Reading it big endian gives a model that does not
    exist."""
    assert discovery.parse(a_reply()).system_id == 0xA59B


def test_unknown_field_types_are_kept_not_fatal() -> None:
    """Every kind of Ubiquiti device answers the same probe."""
    body = b"".join(discovery.encode_field(k, v) for k, v in CAPTURED)
    body += discovery.encode_field(0x7E, b"whatever")
    announcement = discovery.parse(bytes([1, 0]) + len(body).to_bytes(2, "big") + body)
    assert announcement.fields[-1].name == "0x7e"
    assert announcement.fields[-1].value == b"whatever"


def test_a_truncated_reply_yields_what_survived() -> None:
    announcement = discovery.parse(a_reply()[:20])
    assert announcement.fields, "the fields before the cut are still readable"


def test_a_datagram_shorter_than_its_header_is_refused() -> None:
    with pytest.raises(discovery.DiscoveryError):
        discovery.parse(b"\x01\x00")


def test_an_announcement_round_trips() -> None:
    built = discovery.announce(
        mac="AA:BB:CC:DD:EE:FF", ip="192.168.2.50", hostname="G5 PTX",
        platform="UVC G5 PTZ", firmware="UVC.SAV530q.v5.3.95.67", system_id=0xA59B,
        uptime=34306, device_id="11112222-3333-4444-5555-666677778888",
        guid=bytes(range(16)),
    )
    again = discovery.parse(built.to_bytes())
    assert again.mac == "aa:bb:cc:dd:ee:ff"
    assert again.ip == "192.168.2.50"
    assert again.system_id == 0xA59B
    assert again.platform == "UVC G5 PTZ"
    device_id = again.first(discovery.DEVICE_ID)
    assert device_id is not None and device_id.text.startswith("11112222")


def test_guid_is_rendered_the_conventional_way() -> None:
    built = discovery.announce(
        mac="AABBCCDDEEFF", ip="10.0.0.1", hostname="h", platform="p", firmware="f",
        system_id=1, guid=bytes.fromhex("0123456789abcdef0123456789abcdef"),
    )
    found = discovery.parse(built.to_bytes()).first(discovery.GUID)
    assert found is not None
    assert found.uuid == "01234567-89ab-cdef-0123-456789abcdef"


def test_a_malformed_mac_is_refused() -> None:
    with pytest.raises(discovery.DiscoveryError):
        discovery.mac_bytes("not-a-mac")
