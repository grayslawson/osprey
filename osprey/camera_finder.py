"""Find UniFi cameras on the LAN with Ubiquiti's own discovery (UDP 10001).

The setup wizard broadcasts the four-byte probe from ``unifiwire.discovery``
and lists every camera that answers, with its model, firmware and whether a
controller already manages it. A typed IP address gets the same probe as a
unicast, which also works across a container bridge.

Replies are untrusted LAN input: each is bounded in size, parsed with the
tolerant TLV reader, and every string is cut short and stripped of control
characters before it reaches the browser.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Iterable
from typing import Final

from firmware import firmware_status, release_of
from unifiwire import discovery as wire

MAX_DATAGRAM: Final = 1500
MAX_DEVICES: Final = 64
MAX_TEXT: Final = 96
SCAN_SEC: Final = 2.5
PROBE_SEC: Final = 1.5
BROADCAST_TARGETS: Final = ((wire.BROADCAST, wire.PORT), (wire.MULTICAST_GROUP, wire.PORT))


def _text(value: str) -> str:
    return "".join(ch for ch in value if ch.isprintable())[:MAX_TEXT].strip()


def describe(datagram: bytes, sender: str) -> dict[str, object] | None:
    """One discovery reply as the wizard shows it, or None if it is not a camera reply."""
    if len(datagram) > MAX_DATAGRAM or wire.is_probe(datagram):
        return None
    try:
        found = wire.parse(datagram)
        mac = found.mac.upper()
        ip = found.ip or sender
    except (wire.DiscoveryError, ValueError):
        return None
    platform = _text(found.platform)
    # Access points and switches answer the same probe; cameras report a UVC platform.
    if not mac or not platform.upper().startswith("UVC"):
        return None
    firmware = _text(found.firmware)
    status, message = firmware_status(firmware)
    default = found.value(wire.MGMT_IS_DEFAULT)
    # 0 once a controller has adopted it (measured on a Protect-managed G5 PTZ);
    # absent means the camera did not say.
    managed = None if default is None else int.from_bytes(default, "big") == 0
    return {
        "mac": mac,
        "ip": ip,
        "model": platform,
        "hostname": _text((found.value(wire.HOSTNAME) or b"").decode("utf-8", errors="replace")),
        "firmware": firmware,
        "firmware_release": release_of(firmware) or "",
        "firmware_status": status,
        "firmware_message": message,
        "managed": managed,
        "ptz": "PTZ" in platform.upper(),
    }


def _collect(sock: socket.socket, seconds: float, resend: Iterable[tuple[str, int]],
             limit: int = MAX_DEVICES) -> dict[str, dict[str, object]]:
    targets = tuple(resend)
    found: dict[str, dict[str, object]] = {}
    deadline = time.monotonic() + seconds
    next_send = 0.0
    while len(found) < limit:
        now = time.monotonic()
        if now >= deadline:
            break
        if now >= next_send:  # UDP is lossy: probe three times over the window
            for target in targets:
                try:
                    sock.sendto(wire.probe(), target)
                except OSError:
                    pass  # no route for one target (multicast) must not end the scan
            next_send = now + seconds / 3
        sock.settimeout(max(0.05, min(deadline, next_send) - now))
        try:
            datagram, (sender, _port, *_) = sock.recvfrom(MAX_DATAGRAM + 1)
        except (TimeoutError, BlockingIOError):
            continue
        except OSError:
            break
        camera = describe(datagram, sender)
        if camera is not None:
            found[str(camera["mac"])] = camera
    return found


def subnet_broadcasts(route_table: str) -> list[str]:
    """The directed broadcast address of each directly connected IPv4 subnet.

    Sent besides 255.255.255.255, which some hosts route only out of the default
    interface, or masquerade on a container bridge. ``route_table`` is the text of
    Linux's ``/proc/net/route`` (addresses in little-endian hex).
    """
    found: list[str] = []
    for line in route_table.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 8 or parts[2] != "00000000":  # on-link routes only, not via a gateway
            continue
        try:
            network = int.from_bytes(bytes.fromhex(parts[1]), "little")
            mask = int.from_bytes(bytes.fromhex(parts[7]), "little")
        except ValueError:
            continue
        if mask in (0, 0xFFFFFFFF) or network >> 24 == 127:
            continue
        address = str(ipaddress.IPv4Address(network | (~mask & 0xFFFFFFFF)))
        if address not in found:
            found.append(address)
    return found


def _default_targets() -> list[tuple[str, int]]:
    try:
        with open("/proc/net/route", encoding="ascii") as handle:
            table = handle.read()
    except OSError:
        table = ""
    return [*BROADCAST_TARGETS, *((address, wire.PORT) for address in subnet_broadcasts(table))]


def scan(seconds: float = SCAN_SEC, targets: Iterable[tuple[str, int]] | None = None) -> list[dict[str, object]]:
    """Broadcast the probe and return the cameras that answered, sorted by IP."""
    targets = _default_targets() if targets is None else targets
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.bind(("0.0.0.0", 0))
        found = _collect(sock, seconds, targets)
    return sorted(found.values(), key=lambda c: ipaddress.ip_address(str(c["ip"])))


def probe(ip: str, seconds: float = PROBE_SEC, port: int = wire.PORT) -> dict[str, object] | None:
    """Ask one address directly; the camera at ``ip``, or None if nothing answered."""
    address = str(ipaddress.IPv4Address(ip))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("0.0.0.0", 0))
        found = _collect(sock, seconds, [(address, port)], limit=1)
    return next(iter(found.values()), None)
