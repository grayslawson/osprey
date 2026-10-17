#!/usr/bin/env python3
"""Verify the vendored browser modules in cuckoo/web/vendor.

Default (offline): every file listed in manifest.json exists and matches its
recorded sha256, and no unlisted file is present.

``--fetch`` (network, opt-in): also download each npm tarball, check the
registry's sha512 integrity string, and confirm every vendored file is a
byte-identical copy of the named tarball member.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "cuckoo" / "web" / "vendor"


def verify(fetch: bool = False, vendor: Path = VENDOR) -> list[str]:
    problems: list[str] = []
    manifest = json.loads((vendor / "manifest.json").read_text(encoding="utf-8"))
    listed = {"manifest.json"}
    for package in manifest["packages"]:
        members: dict[str, bytes] = {}
        if fetch:
            with urllib.request.urlopen(package["tarball"], timeout=30) as response:
                blob = response.read()
            algorithm, _, expected = package["integrity"].partition("-")
            digest = base64.b64encode(hashlib.new(algorithm, blob).digest()).decode()
            if digest != expected:
                problems.append(f"{package['package']}: tarball integrity mismatch")
            if hashlib.sha256(blob).hexdigest() != package["tarball_sha256"]:
                problems.append(f"{package['package']}: tarball sha256 mismatch")
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
                for name in (entry["from"] for entry in package["files"].values()):
                    member = archive.extractfile(name)
                    members[name] = member.read() if member is not None else b""
        for relative, entry in package["files"].items():
            listed.add(relative)
            path = vendor / relative
            if not path.is_file():
                problems.append(f"missing vendored file: {relative}")
                continue
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                problems.append(f"checksum mismatch: {relative}")
            if fetch and members.get(entry["from"]) != data:
                problems.append(f"{relative} differs from {package['tarball']}#{entry['from']}")
    for path in vendor.rglob("*"):
        if path.is_file() and path.relative_to(vendor).as_posix() not in listed:
            problems.append(f"unlisted vendored file: {path.relative_to(vendor)}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fetch", action="store_true", help="also re-download and compare npm tarballs")
    args = parser.parse_args()
    problems = verify(fetch=args.fetch)
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print("web vendor files verified" + (" against npm" if args.fetch else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
