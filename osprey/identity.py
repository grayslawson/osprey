"""The ONVIF device identity Osprey presents northbound.

Osprey is a *separate* ONVIF device that happens to re-publish a UniFi camera.
It must never present the physical camera's MAC address or serial number: UniFi
Protect can list the physical G5 (via UniFi discovery) and Osprey's ONVIF
persona at the same time, and identical identifiers invite Protect to merge or
conflict the two records.

Every value is configurable under ``onvif.identity`` in the runtime JSON. When
unset, values are *derived* from a stable seed (the camera MAC when known), so
they survive restarts but are never equal to the camera's own identifiers:

* ``serial`` — ``OSP`` plus 12 hex digits of SHA-256 over the seed;
* ``mac`` — a locally administered unicast MAC (``x2:…``) from the same hash,
  or the MAC of ``discovery.interface`` when that interface exists;
* ``model`` — ``Osprey <camera model>``;
* ``hardware_id`` — ``osprey-onvif``;
* ``hostname`` — the controller name reduced to an RFC 1123 label.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

DEFAULT_MANUFACTURER: Final = "Osprey"
DEFAULT_HARDWARE_ID: Final = "osprey-onvif"
SYSFS_NET: Final = "/sys/class/net"

_TEXT_RE: Final = re.compile(r"[\x20-\x7e]{1,64}")
_MAC_RE: Final = re.compile(r"[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2}){5}")
_HOST_RE: Final = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_IFACE_RE: Final = re.compile(r"[A-Za-z0-9_.:@-]{1,15}")


def normalise_mac(value: str) -> str:
    digits = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
    if len(digits) != 12:
        raise ValueError("a MAC address needs 12 hexadecimal digits")
    return ":".join(digits[index:index + 2] for index in range(0, 12, 2))


def derived_mac(seed: str) -> str:
    raw = bytearray(hashlib.sha256(f"osprey-onvif-mac\0{seed}".encode()).digest()[:6])
    raw[0] = (raw[0] & 0xFC) | 0x02  # locally administered, unicast
    return ":".join(f"{byte:02X}" for byte in raw)


def derived_serial(seed: str) -> str:
    return "OSP" + hashlib.sha256(f"osprey-onvif-serial\0{seed}".encode()).hexdigest()[:12].upper()


def hostname_label(name: str) -> str:
    label = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:63].strip("-")
    return label or "osprey"


def interface_mac(name: str, sysfs: str = SYSFS_NET) -> str | None:
    """The MAC of a local interface (Linux sysfs), or None when unavailable."""
    if _IFACE_RE.fullmatch(name) is None:
        return None
    try:
        value = (Path(sysfs) / name / "address").read_text(encoding="ascii").strip()
        mac = normalise_mac(value)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return None if mac == "00:00:00:00:00:00" else mac


@dataclass(frozen=True)
class IdentityConfig:
    """Operator overrides; ``None`` means derive."""

    manufacturer: str = DEFAULT_MANUFACTURER
    model: str | None = None
    serial: str | None = None
    hardware_id: str | None = None
    mac: str | None = None
    hostname: str | None = None
    prefix_length: int = 24

    @classmethod
    def from_mapping(cls, value: Any) -> "IdentityConfig":
        if value in (None, {}):
            return cls()
        if not isinstance(value, dict):
            raise ValueError('"onvif.identity" must be an object')
        allowed = {"manufacturer", "model", "serial", "hardware_id", "mac", "hostname", "prefix_length"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f'"onvif.identity" has unknown keys: {", ".join(sorted(unknown))}')
        text: dict[str, str | None] = {}
        for key in ("manufacturer", "model", "serial", "hardware_id"):
            item = value.get(key)
            if item is not None and (not isinstance(item, str) or _TEXT_RE.fullmatch(item) is None):
                raise ValueError(f"onvif.identity.{key} must be 1-64 printable ASCII characters")
            text[key] = item
        mac = value.get("mac")
        if mac is not None:
            if not isinstance(mac, str) or _MAC_RE.fullmatch(mac.strip()) is None:
                raise ValueError("onvif.identity.mac must be a MAC address")
            mac = normalise_mac(mac)
            if int(mac[:2], 16) & 0x01:
                raise ValueError("onvif.identity.mac must be a unicast MAC address")
        hostname = value.get("hostname")
        if hostname is not None and (not isinstance(hostname, str) or _HOST_RE.fullmatch(hostname) is None):
            raise ValueError("onvif.identity.hostname must be an RFC 1123 host label")
        prefix = value.get("prefix_length", 24)
        if isinstance(prefix, bool) or not isinstance(prefix, int) or not 1 <= prefix <= 32:
            raise ValueError("onvif.identity.prefix_length must be an integer from 1 to 32")
        return cls(
            manufacturer=text["manufacturer"] or DEFAULT_MANUFACTURER,
            model=text["model"],
            serial=text["serial"],
            hardware_id=text["hardware_id"],
            mac=mac,
            hostname=hostname,
            prefix_length=prefix,
        )


@dataclass(frozen=True)
class DeviceIdentity:
    manufacturer: str
    model: str
    serial: str
    hardware_id: str
    mac: str
    hostname: str
    prefix_length: int = 24


def resolve(
    config: IdentityConfig,
    *,
    seed: str,
    camera_mac: str | None = None,
    camera_model: str | None = None,
    controller_name: str = "osprey",
    interface_hw: str | None = None,
) -> DeviceIdentity:
    """Combine overrides with derived values; never return the camera's MAC."""
    forbidden = normalise_mac(camera_mac) if camera_mac and len(re.sub(r"[^0-9A-Fa-f]", "", camera_mac)) == 12 else None
    mac = config.mac or interface_hw or derived_mac(seed)
    if forbidden is not None and mac == forbidden:
        mac = derived_mac(seed)
    serial = config.serial or derived_serial(seed)
    if forbidden is not None and re.sub(r"[^0-9A-F]", "", serial.upper()) == forbidden.replace(":", ""):
        serial = derived_serial(seed)
    model = config.model or (f"Osprey {camera_model}" if camera_model else "Osprey PTZ")
    return DeviceIdentity(
        manufacturer=config.manufacturer,
        model=model[:64],
        serial=serial,
        hardware_id=config.hardware_id or DEFAULT_HARDWARE_ID,
        mac=mac,
        hostname=config.hostname or hostname_label(controller_name),
        prefix_length=config.prefix_length,
    )
