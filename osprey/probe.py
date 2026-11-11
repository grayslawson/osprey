"""``osprey probe``: gather what is needed to support another UniFi PTZ model.

Read-only. It sends one UniFi discovery request (UDP 10001) to the camera and,
optionally, reads a message dump written by ``osprey --dump FILE`` while that
camera was connected. It never logs in to, configures, moves or adopts a camera.

The output is one JSON bundle with addresses, MACs, serials, UUIDs, tokens,
names and credentials replaced by placeholders, so it can be attached to a
GitHub issue. Read it before you share it.

    osprey probe --camera 192.0.2.20 [--dump messages.jsonl] [--out bundle.json]
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import socket
import sys
from pathlib import Path
from typing import Any, Final

from unifiwire import discovery
from unifiwire.envelope import HELLO, PARAM_AGREEMENT

import model

BUNDLE_VERSION: Final = 1
DISCOVERY_PORT: Final = 10001
# Per function name, keep the first few messages the camera sent: enough to see
# shapes (motor state, settings replies) without shipping a whole session.
SAMPLES_PER_FUNCTION: Final = 3
MAX_DUMP_LINES: Final = 200_000

REDACTED: Final = "<redacted>"
# Keys whose values identify a person, place, device or secret.
SENSITIVE_KEY: Final = re.compile(
    r"mac|ip|host|serial|guid|uuid|token|pass|secret|key|auth|cert|ssid|"
    r"address|url|destination|device.?id|user|email|name|wifi|location|"
    r"latitude|longitude|timezone",
    re.IGNORECASE,
)
# Keys that match the pattern above but describe the protocol, not the user.
SAFE_KEYS: Final = frozenset({"functionName", "streamName", "lensmodel", "codecName"})
VALUE_PATTERNS: Final = (
    # Not inside dotted firmware versions such as "v5.3.95.67.148b9a3".
    (re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"), "<ipv4>"),
    (re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b"), "<mac>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"^[0-9A-Fa-f]{12}$"), "<mac>"),
)


def redact(value: Any, key: str = "") -> Any:
    """Return a copy of ``value`` with identifying data replaced."""
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if key and key not in SAFE_KEYS and SENSITIVE_KEY.search(key) and not isinstance(value, bool):
        return REDACTED if value not in (None, "") else value
    if isinstance(value, str):
        for pattern, placeholder in VALUE_PATTERNS:
            value = pattern.sub(placeholder, value)
    return value


def discover(camera: str, timeout: float) -> dict[str, Any]:
    """Ask one camera for its UniFi discovery record (unicast, one datagram)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(discovery.probe(), (camera, DISCOVERY_PORT))
        while True:
            data, (peer, _) = sock.recvfrom(4096)
            if peer != camera or discovery.is_probe(data):
                continue
            found = discovery.parse(data)
            return {
                "answered": True,
                "platform": found.platform,
                "firmware": found.firmware,
                "system_id": None if found.system_id is None else f"0x{found.system_id:04x}",
                "fields": sorted({f.name for f in found.fields}),
            }
    except TimeoutError:
        # Seen on an adopted G5 PTZ (firmware 5.3.x): no unicast answer at all. The
        # message dump is the reliable source; discovery is best effort.
        return {"answered": False, "note": "no reply on UDP 10001 within the timeout "
                "(adopted cameras may not answer unicast probes; use --dump)"}
    except (OSError, discovery.DiscoveryError) as exc:
        return {"answered": False, "note": f"{type(exc).__name__}: {exc}"}
    finally:
        sock.close()


def read_dump(path: Path) -> dict[str, Any]:
    """Pick the camera's hello, parameter agreement and a few samples of the rest."""
    hello: dict[str, Any] | None = None
    agreement: dict[str, Any] | None = None
    samples: dict[str, list[Any]] = {}
    counts: dict[str, int] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle):
            if number >= MAX_DUMP_LINES:
                break
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or item.get("direction") != "in":
                continue
            name = str(item.get("functionName") or "?")
            payload = item.get("payload")
            counts[name] = counts.get(name, 0) + 1
            if name == HELLO and hello is None and isinstance(payload, dict):
                hello = payload
            elif name == PARAM_AGREEMENT and agreement is None:
                agreement = payload
            elif len(samples.setdefault(name, [])) < SAMPLES_PER_FUNCTION:
                samples[name].append(payload)
    return {"hello": hello, "param_agreement": agreement, "counts": counts, "samples": samples}


def summarise(found: dict[str, Any], dump: dict[str, Any] | None) -> dict[str, Any]:
    """The few facts a maintainer looks at first."""
    raw = (dump or {}).get("hello")
    hello: dict[str, Any] = raw if isinstance(raw, dict) else {}
    found_features = hello.get("features")
    features: dict[str, Any] = found_features if isinstance(found_features, dict) else {}

    def steps(axis: str) -> Any:
        item = features.get(axis)
        return item.get("steps") if isinstance(item, dict) else None

    lens = str(hello.get("lensmodel") or "")
    platform = str(found.get("platform") or "")
    names = {lens.strip().casefold(), platform.strip().casefold()} - {""}
    return {
        "platform": platform or None,
        "lensmodel": lens or None,
        "firmware": hello.get("fwVersion") or found.get("firmware"),
        "system_id": found.get("system_id"),
        "ptz_steps": {axis: steps(axis) for axis in ("pan", "tilt", "zoom")},
        "support": "supported" if names & set(model.SUPPORTED_MODELS) else "experimental",
    }


def build(camera: str | None, dump_path: Path | None, timeout: float) -> dict[str, Any]:
    found = discover(camera, timeout) if camera else {"answered": False, "note": "no --camera given"}
    dump = read_dump(dump_path) if dump_path else None
    bundle = {
        "bundle_version": BUNDLE_VERSION,
        "osprey_version": _version(),
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": summarise(found, dump),
        "discovery": found,
        "dump": dump,
    }
    out: dict[str, Any] = redact(bundle)
    return out


def _version() -> str:
    for candidate in (Path(__file__).resolve().parent.parent / "VERSION", Path("/opt/osprey/VERSION")):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="osprey probe",
        description="Read-only data gathering for an unsupported UniFi PTZ model. "
        "Writes a redacted JSON bundle to attach to a GitHub issue.",
    )
    parser.add_argument("--camera", help="camera IPv4 address for the UDP 10001 discovery request")
    parser.add_argument("--dump", type=Path, help="message dump from `osprey --dump FILE`")
    parser.add_argument("--out", type=Path, help="write the bundle here (default: stdout)")
    parser.add_argument("--timeout", type=float, default=3.0, help="discovery wait in seconds")
    args = parser.parse_args(argv)
    if not args.camera and not args.dump:
        parser.error("give --camera, --dump, or both")
    if args.dump and not args.dump.is_file():
        parser.error(f"dump file not found: {args.dump}")
    bundle = build(args.camera, args.dump, args.timeout)
    text = json.dumps(bundle, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}; review it before attaching it to an issue", file=sys.stderr)
    else:
        sys.stdout.write(text)
    summary = bundle["summary"]
    print(f"model: {summary['platform'] or summary['lensmodel'] or 'unknown'} ({summary['support']})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
