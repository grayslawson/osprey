"""Device discovery on UDP 10001 — how a controller finds devices before adoption.

This is Ubiquiti's own discovery protocol, shared with access points and switches
rather than specific to cameras. A controller broadcasts a four-byte probe; every
device on the segment answers with a TLV block describing itself, and the operator
picks one to adopt.

    probe   01 00 00 00                       version 1, command 0, no payload
    reply   01 00 00 aa <tlv><tlv>…           same header, length, then fields

Each field is a one-byte type, a two-byte big-endian length, and that many bytes.
Unknown types are common — devices of every kind answer the same probe — so a
reader keeps what it recognises and steps over the rest rather than failing.

Everything below was decoded from a live exchange between a real controller and a
real camera. Field names for types nobody has observed in the wild are left out
rather than guessed.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Final, Iterator

PORT: Final = 10001
MULTICAST_GROUP: Final = "233.89.188.1"
BROADCAST: Final = "255.255.255.255"

VERSION: Final = 1
COMMAND_PROBE: Final = 0

HEADER_LEN: Final = 4
FIELD_HEADER_LEN: Final = 3

# Field types observed in a real camera's reply.
HWADDR: Final = 0x01  # 6 bytes, the MAC
IPINFO: Final = 0x02  # 10 bytes, MAC + IPv4
FWVERSION: Final = 0x03  # text, e.g. "UVC.SAV530q.v5.3.95.67.…"
UPTIME: Final = 0x0A  # 4 bytes, seconds
HOSTNAME: Final = 0x0B  # text
PLATFORM: Final = 0x0C  # text, e.g. "UVC G5 PTZ"
MGMT_IS_DEFAULT: Final = 0x17  # 4 bytes, boolean-ish
SYSTEM_ID: Final = 0x10  # 2 bytes, the model id — LITTLE endian
DEVICE_ID: Final = 0x20  # text UUID, the same value the camera sends as `device-id`
GUID: Final = 0x2B  # 16 raw bytes, the same value it sends as `x-guid`
DEFAULT_CREDENTIALS: Final = 0x2C  # 1 byte

NAMES: Final[dict[int, str]] = {
    HWADDR: "hwaddr",
    IPINFO: "ipinfo",
    FWVERSION: "firmware",
    UPTIME: "uptime",
    HOSTNAME: "hostname",
    PLATFORM: "platform",
    SYSTEM_ID: "system_id",
    MGMT_IS_DEFAULT: "mgmt_is_default",
    DEVICE_ID: "device_id",
    GUID: "guid",
    DEFAULT_CREDENTIALS: "default_credentials",
}


class DiscoveryError(ValueError):
    pass


def probe() -> bytes:
    """The datagram a controller sends. Four bytes, no payload."""
    return bytes([VERSION, COMMAND_PROBE, 0, 0])


def is_probe(datagram: bytes) -> bool:
    return datagram[:HEADER_LEN] == probe()


def encode_field(kind: int, value: bytes) -> bytes:
    return bytes([kind]) + len(value).to_bytes(2, "big") + value


def mac_bytes(mac: str) -> bytes:
    """Accept `AA:BB:CC:DD:EE:FF`, `aabbccddeeff`, or `AA-BB-…`."""
    cleaned = mac.replace(":", "").replace("-", "").replace(".", "")
    if len(cleaned) != 12:
        raise DiscoveryError(f"not a MAC: {mac!r}")
    return bytes.fromhex(cleaned)


def address_field(mac: str, ip: str) -> bytes:
    """MAC and IPv4 packed together, as `IPINFO` and `PRIMARY_ADDRESS` carry them."""
    return mac_bytes(mac) + ipaddress.IPv4Address(ip).packed


@dataclass(frozen=True)
class Field:
    kind: int
    value: bytes

    @property
    def name(self) -> str:
        return NAMES.get(self.kind, f"0x{self.kind:02x}")

    @property
    def text(self) -> str:
        return self.value.decode("utf-8", errors="replace")

    @property
    def mac(self) -> str:
        """The MAC in a `HWADDR` or an address field."""
        return ":".join(f"{b:02x}" for b in self.value[:6])

    @property
    def ip(self) -> str:
        """The address in a 10-byte address field."""
        if len(self.value) < 10:
            raise DiscoveryError(f"{self.name} carries no address")
        return str(ipaddress.IPv4Address(self.value[6:10]))

    @property
    def number(self) -> int:
        return int.from_bytes(self.value, "big")

    @property
    def uuid(self) -> str:
        """A 16-byte GUID rendered the conventional way."""
        raw = self.value.hex()
        if len(raw) != 32:
            return self.text
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


@dataclass
class Announcement:
    """One device's answer, as a list of fields plus a few conveniences."""

    version: int = VERSION
    command: int = COMMAND_PROBE
    fields: list[Field] = field(default_factory=list)

    def first(self, kind: int) -> Field | None:
        return next((f for f in self.fields if f.kind == kind), None)

    def value(self, kind: int) -> bytes | None:
        found = self.first(kind)
        return found.value if found else None

    @property
    def mac(self) -> str:
        """The device's MAC, from whichever field carries it."""
        for kind in (HWADDR, IPINFO):
            found = self.first(kind)
            if found is not None and len(found.value) >= 6:
                return found.mac
        return ""

    @property
    def ip(self) -> str:
        found = self.first(IPINFO)
        return found.ip if found is not None and len(found.value) >= 10 else ""

    @property
    def platform(self) -> str:
        found = self.first(PLATFORM)
        return found.text if found is not None else ""

    @property
    def firmware(self) -> str:
        found = self.first(FWVERSION)
        return found.text if found is not None else ""

    @property
    def system_id(self) -> int | None:
        """The model id.

        Stored **little endian** here, while the same value appears big endian in
        the `camera-model` WebSocket header — `9b a5` on the wire is `0xa59b`.
        """
        found = self.first(SYSTEM_ID)
        return int.from_bytes(found.value, "little") if found is not None else None

    def to_bytes(self) -> bytes:
        body = b"".join(encode_field(f.kind, f.value) for f in self.fields)
        return bytes([self.version, self.command]) + len(body).to_bytes(2, "big") + body


def parse_fields(payload: bytes) -> Iterator[Field]:
    """Walk TLVs, stopping cleanly at a truncated tail."""
    at = 0
    while at + FIELD_HEADER_LEN <= len(payload):
        kind = payload[at]
        length = int.from_bytes(payload[at + 1 : at + 3], "big")
        at += FIELD_HEADER_LEN
        if at + length > len(payload):
            return
        yield Field(kind=kind, value=payload[at : at + length])
        at += length


def parse(datagram: bytes) -> Announcement:
    """Read an announcement. Raises only when the header itself is unusable."""
    if len(datagram) < HEADER_LEN:
        raise DiscoveryError("datagram is shorter than its header")
    stated = int.from_bytes(datagram[2:4], "big")
    body = datagram[HEADER_LEN : HEADER_LEN + stated] if stated else datagram[HEADER_LEN :]
    return Announcement(
        version=datagram[0], command=datagram[1], fields=list(parse_fields(body))
    )


def announce(
    mac: str,
    ip: str,
    hostname: str,
    platform: str,
    firmware: str,
    system_id: int,
    uptime: int = 0,
    device_id: str = "",
    guid: bytes = b"",
) -> Announcement:
    """Build the reply a device sends, in the order a real camera sends it."""
    fields = [
        Field(IPINFO, address_field(mac, ip)),
        Field(HWADDR, mac_bytes(mac)),
        Field(UPTIME, uptime.to_bytes(4, "big")),
        Field(HOSTNAME, hostname.encode()),
        Field(PLATFORM, platform.encode()),
        Field(MGMT_IS_DEFAULT, (0).to_bytes(4, "big")),
        Field(FWVERSION, firmware.encode()),
        Field(SYSTEM_ID, system_id.to_bytes(2, "little")),
    ]
    if device_id:
        fields.append(Field(DEVICE_ID, device_id.encode()))
    if guid:
        fields.append(Field(GUID, guid))
    fields.append(Field(DEFAULT_CREDENTIALS, bytes([3])))
    return Announcement(fields=fields)
