"""``osprey protect-guard``: keep UniFi Protect from adopting the physical camera.

While Osprey controls a UniFi PTZ camera, a Protect console on the same network
still sees the camera through UniFi discovery and offers to adopt it. Adopting
it would take the camera away from Osprey. The guard is one firewall policy on
the UniFi gateway: block the camera's own traffic to the gateway on the UniFi
camera ports. Osprey talks to the camera directly on the LAN, so it is unaffected.

``--check`` (default) only reads. ``--apply`` creates the policy when it is
missing. Both use the UniFi Network integration API with an API key from
Settings > Control Plane > Integrations (``--api-key-file`` or
``OSPREY_UNIFI_API_KEY_FILE``). This works when the gateway is also the Protect
console (UniFi Cloud Gateway, UDM, UCG). For a separate NVR, block the camera
with that NVR's own firewall instead (see docs/protect-guard.md).

    osprey protect-guard --console https://192.168.1.1 --camera-ip 192.0.2.20 [--apply]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Final

# Controller, media, snapshot (TCP) and UniFi discovery (UDP).
GUARD_PORTS: Final = (7442, 7444, 7550, 10001)
# The two the camera needs to be adopted; a policy blocking these is enough.
REQUIRED_PORTS: Final = frozenset({7442, 7550})
POLICY_NAME: Final = "Osprey guard: camera to Protect"
PAGE: Final = 200

Request = Callable[[str, str, dict[str, Any] | None], Any]


def http_client(console: str, api_key: str, verify: bool) -> Request:
    """A tiny JSON client for ``{console}/proxy/network/integration/v1``."""
    base = console.rstrip("/") + "/proxy/network/integration/v1"
    context = ssl.create_default_context()
    if not verify:
        # UniFi consoles ship a self-signed certificate.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    def request(method: str, path: str, body: dict[str, Any] | None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers={
            "X-API-KEY": api_key, "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        with urllib.request.urlopen(req, timeout=15, context=context) as response:
            return json.loads(response.read() or b"null")

    return request


def _all(request: Request, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = request("GET", f"{path}?offset={offset}&limit={PAGE}", None)
        data = page.get("data") or []
        items.extend(data)
        offset += len(data)
        if not data or offset >= int(page.get("totalCount") or 0):
            return items


def _ports(policy: dict[str, Any]) -> set[int]:
    flt = (policy.get("destination") or {}).get("trafficFilter") or {}
    port_filter = flt.get("portFilter") or {}
    if port_filter.get("matchOpposite"):
        return set()
    out: set[int] = set()
    for item in port_filter.get("items") or []:
        if item.get("type") == "PORT_NUMBER":
            out.add(int(item["value"]))
        elif item.get("type") == "PORT_NUMBER_RANGE":
            out.update(range(int(item["start"]), int(item["stop"]) + 1))
    return out


def _sources(policy: dict[str, Any]) -> set[str]:
    flt = (policy.get("source") or {}).get("trafficFilter") or {}
    ip_filter = flt.get("ipAddressFilter") or {}
    if flt.get("type") != "IP_ADDRESS" or ip_filter.get("matchOpposite"):
        return set()
    return {str(item.get("value")) for item in ip_filter.get("items") or []}


def find_guard(policies: list[dict[str, Any]], camera_ip: str, gateway_zone: str) -> dict[str, Any] | None:
    """An enabled BLOCK/REJECT policy from the camera to the gateway on the adoption ports."""
    for policy in policies:
        if not policy.get("enabled") or (policy.get("action") or {}).get("type") not in ("BLOCK", "REJECT"):
            continue
        if (policy.get("destination") or {}).get("zoneId") != gateway_zone:
            continue
        if camera_ip in _sources(policy) and REQUIRED_PORTS <= _ports(policy):
            return policy
    return None


def guard_policy(camera_ip: str, source_zone: str, gateway_zone: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "name": POLICY_NAME,
        "description": "Created by osprey protect-guard. Stops UniFi Protect on this console from "
        "adopting the PTZ camera that Osprey controls. Delete it to hand the camera back to Protect.",
        "action": {"type": "BLOCK"},
        "source": {"zoneId": source_zone, "trafficFilter": {"type": "IP_ADDRESS", "ipAddressFilter": {
            "type": "IP_ADDRESSES", "matchOpposite": False,
            "items": [{"type": "IP_ADDRESS", "value": camera_ip}]}}},
        "destination": {"zoneId": gateway_zone, "trafficFilter": {"type": "PORT", "portFilter": {
            "type": "PORTS", "matchOpposite": False,
            "items": [{"type": "PORT_NUMBER", "value": port} for port in GUARD_PORTS]}}},
        "ipProtocolScope": {"ipVersion": "IPV4", "protocolFilter": {"type": "PRESET", "preset": {"name": "TCP_UDP"}}},
        "loggingEnabled": True,
    }


def run(request: Request, camera_ip: str, site: str, apply: bool) -> tuple[int, str]:
    """Return (exit code, message): 0 guarded, 1 missing, 2 cannot tell."""
    sites = _all(request, "/sites")
    match = [s for s in sites if site in (s.get("internalReference"), s.get("name"), s.get("id"))]
    if not match:
        return 2, f"site {site!r} not found"
    site_path = f"/sites/{match[0]['id']}"
    zones = {z.get("name"): z.get("id") for z in _all(request, site_path + "/firewall/zones")}
    gateway, internal = zones.get("Gateway"), zones.get("Internal")
    if not gateway or not internal:
        return 2, "this console has no zone-based firewall (Gateway/Internal zones); see docs/protect-guard.md"
    policies = _all(request, site_path + "/firewall/policies")
    found = find_guard(policies, camera_ip, gateway)
    if found is not None:
        return 0, f"guarded by policy {found.get('id')} ({found.get('name')!r})"
    if not apply:
        return 1, "no guard policy found; run again with --apply, or follow docs/protect-guard.md"
    created = request("POST", site_path + "/firewall/policies", guard_policy(camera_ip, internal, gateway))
    return 0, f"created policy {created.get('id')} ({POLICY_NAME!r})"


def _api_key(path: str | None) -> str:
    source = path or os.environ.get("OSPREY_UNIFI_API_KEY_FILE")
    if not source:
        raise SystemExit("give --api-key-file or set OSPREY_UNIFI_API_KEY_FILE")
    key = Path(source).read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"API key file is empty: {source}")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="osprey protect-guard",
        description="Check for (or create) the UniFi firewall policy that keeps Protect "
        "from adopting the camera Osprey controls.",
    )
    parser.add_argument("--console", required=True, help="UniFi console URL, e.g. https://192.168.1.1")
    parser.add_argument("--camera-ip", required=True, help="the physical camera's (reserved) IPv4 address")
    parser.add_argument("--api-key-file", help="file holding a UniFi Network API key")
    parser.add_argument("--site", default="default", help="site name or id (default: default)")
    parser.add_argument("--apply", action="store_true", help="create the policy if it is missing")
    parser.add_argument("--verify-tls", action="store_true", help="verify the console certificate")
    args = parser.parse_args(argv)
    camera_ip = str(ipaddress.IPv4Address(args.camera_ip))
    request = http_client(args.console, _api_key(args.api_key_file), args.verify_tls)
    try:
        code, message = run(request, camera_ip, args.site, args.apply)
    except urllib.error.HTTPError as exc:
        code, message = 2, f"UniFi API answered HTTP {exc.code} (check the key's permissions)"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        code, message = 2, f"cannot reach the UniFi API: {exc}"
    print(("OK      " if code == 0 else "WARNING ") + message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
