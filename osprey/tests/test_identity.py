"""ONVIF identity: derived values never collide with the physical camera."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import identity
from identity import IdentityConfig

CAMERA_MAC = "AA:BB:CC:DD:EE:FF"


def test_derivations_are_stable_and_locally_administered() -> None:
    assert identity.derived_mac("x") == identity.derived_mac("x") != identity.derived_mac("y")
    first = int(identity.derived_mac("x")[:2], 16)
    assert first & 0x02 and not first & 0x01
    assert identity.derived_serial("x").startswith("OSP") and len(identity.derived_serial("x")) == 15


@pytest.mark.parametrize("name, label", [
    ("Front Gate!", "front-gate"), ("--", "osprey"), ("a" * 100, "a" * 63),
])
def test_hostname_label(name: str, label: str) -> None:
    assert identity.hostname_label(name) == label


def test_normalise_mac() -> None:
    assert identity.normalise_mac("aa-bb-cc-dd-ee-ff") == CAMERA_MAC
    with pytest.raises(ValueError):
        identity.normalise_mac("aa:bb")


def test_interface_mac_reads_sysfs(tmp_path: Path) -> None:
    (tmp_path / "eth0").mkdir()
    (tmp_path / "eth0" / "address").write_text("02:00:00:00:00:01\n")
    (tmp_path / "lo").mkdir()
    (tmp_path / "lo" / "address").write_text("00:00:00:00:00:00\n")
    assert identity.interface_mac("eth0", str(tmp_path)) == "02:00:00:00:00:01"
    assert identity.interface_mac("lo", str(tmp_path)) is None  # all zeros is no MAC
    assert identity.interface_mac("missing", str(tmp_path)) is None
    assert identity.interface_mac("../etc", str(tmp_path)) is None  # not an interface name


@pytest.mark.parametrize("bad", [
    "text", {"bogus": 1}, {"serial": 5}, {"serial": "é"}, {"mac": "nope"}, {"mac": "01:00:00:00:00:00"},
    {"hostname": "-bad-"}, {"prefix_length": 0}, {"prefix_length": True}, {"prefix_length": 33},
])
def test_config_rejects_bad_values(bad: Any) -> None:
    with pytest.raises(ValueError):
        IdentityConfig.from_mapping(bad)


def test_config_accepts_overrides() -> None:
    cfg = IdentityConfig.from_mapping(
        {"model": "M", "serial": "S1", "mac": "02-00-00-00-00-09", "hostname": "cam-1", "prefix_length": 16}
    )
    assert (cfg.model, cfg.serial, cfg.mac, cfg.hostname, cfg.prefix_length) == (
        "M", "S1", "02:00:00:00:00:09", "cam-1", 16)
    assert IdentityConfig.from_mapping(None) == IdentityConfig()


def test_resolve_never_returns_the_cameras_own_mac_or_serial() -> None:
    cfg = IdentityConfig(mac=CAMERA_MAC, serial="AABBCCDDEEFF")
    ident = identity.resolve(cfg, seed=CAMERA_MAC, camera_mac=CAMERA_MAC, camera_model="G5 PTZ")
    assert ident.mac != CAMERA_MAC and ident.serial != "AABBCCDDEEFF"
    assert ident.model == "Osprey G5 PTZ"


def test_resolve_prefers_overrides_then_interface_then_derived() -> None:
    cfg = IdentityConfig(mac="02:00:00:00:00:09", serial="S1", hostname="h")
    ident = identity.resolve(cfg, seed="s", interface_hw="02:00:00:00:00:01")
    assert (ident.mac, ident.serial, ident.hostname) == ("02:00:00:00:00:09", "S1", "h")
    ident = identity.resolve(IdentityConfig(), seed="s", interface_hw="02:00:00:00:00:01", controller_name="My Cam")
    assert ident.mac == "02:00:00:00:00:01" and ident.hostname == "my-cam"
    assert identity.resolve(IdentityConfig(), seed="s").mac == identity.derived_mac("s")


def test_personas_are_distinct_devices_and_ignore_primary_overrides() -> None:
    cfg = IdentityConfig(mac="02:00:00:00:00:09", serial="S1", hostname="h", model="Shared")
    primary = identity.resolve(cfg, seed="s", interface_hw="02:00:00:00:00:01")
    media = identity.resolve(cfg, seed="s", interface_hw="02:00:00:00:00:01", persona="media",
                             controller_name="osprey")
    assert media.mac != primary.mac and media.serial != primary.serial
    assert media.hostname == "osprey-media"
    assert media.model == primary.model == "Shared"  # product description is shared
