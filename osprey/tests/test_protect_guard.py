"""``osprey protect-guard`` against a fake UniFi Network integration API."""

from __future__ import annotations

from typing import Any

import protect_guard

CAMERA = "192.0.2.20"
ZONES = [{"id": "z-gw", "name": "Gateway"}, {"id": "z-in", "name": "Internal"}, {"id": "z-ex", "name": "External"}]


class FakeApi:
    def __init__(self, policies: list[dict[str, Any]], zones: list[dict[str, Any]] = ZONES) -> None:
        self.policies = policies
        self.zones = zones
        self.created: list[dict[str, Any]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        route, _, query = path.partition("?")
        offset = int(dict(p.split("=") for p in query.split("&")).get("offset", 0)) if query else 0
        if method == "POST" and route == "/sites/s1/firewall/policies":
            assert body is not None
            self.created.append(body)
            return {"id": "new-policy", **body}
        table = {
            "/sites": [{"id": "s1", "internalReference": "default", "name": "Default"}],
            "/sites/s1/firewall/zones": self.zones,
            "/sites/s1/firewall/policies": self.policies,
        }[route]
        page = table[offset:offset + 2]  # small pages prove pagination
        return {"data": page, "totalCount": len(table)}


def test_missing_guard_is_reported_then_created() -> None:
    api = FakeApi([{"enabled": True, "name": "unrelated", "action": {"type": "ALLOW"}}] * 3)
    assert protect_guard.run(api, CAMERA, "default", apply=False)[0] == 1
    assert api.created == []
    code, message = protect_guard.run(api, CAMERA, "default", apply=True)
    assert code == 0 and "new-policy" in message
    policy = api.created[0]
    assert policy["action"]["type"] == "BLOCK"
    assert policy["source"]["zoneId"] == "z-in" and policy["destination"]["zoneId"] == "z-gw"
    # What it creates is what it later recognises.
    assert protect_guard.find_guard([policy], CAMERA, "z-gw") is policy


def test_existing_hand_made_guard_is_accepted() -> None:
    hand_made = protect_guard.guard_policy(CAMERA, "z-in", "z-gw")
    hand_made["destination"]["trafficFilter"]["portFilter"]["items"] = [
        {"type": "PORT_NUMBER_RANGE", "start": 7440, "stop": 7560}]
    api = FakeApi([hand_made])
    assert protect_guard.run(api, CAMERA, "default", apply=True)[0] == 0
    assert api.created == []


def test_disabled_allow_or_wrong_camera_does_not_count() -> None:
    base = protect_guard.guard_policy(CAMERA, "z-in", "z-gw")
    disabled = {**base, "enabled": False}
    allow = {**base, "action": {"type": "ALLOW"}}
    other = protect_guard.guard_policy("192.0.2.99", "z-in", "z-gw")
    assert protect_guard.find_guard([disabled, allow, other], CAMERA, "z-gw") is None


def test_console_without_zone_firewall_cannot_tell() -> None:
    api = FakeApi([], zones=[])
    assert protect_guard.run(api, CAMERA, "default", apply=True)[0] == 2
    assert api.created == []
