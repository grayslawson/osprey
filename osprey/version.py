"""The Osprey release version, read from the single source of truth.

``VERSION`` at the repository root is that source. A checkout, the container
(/opt/osprey/VERSION) and the Nix package all carry the file next to the
modules; the wheel carries it as package metadata instead.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path


def get_version() -> str:
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        return metadata.version("osprey-ptz")
    except metadata.PackageNotFoundError:
        return "unknown"
