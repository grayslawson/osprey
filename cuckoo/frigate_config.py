"""Validated, non-secret Frigate connection metadata for the operator console."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def validate_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Frigate URL must use http or https and include a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Frigate URL cannot contain credentials, query, or fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Frigate URL port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Frigate URL port is invalid")
    return value


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None

    @classmethod
    def from_environment(cls) -> "Store":
        return cls(os.environ.get("OSPREY_FRIGATE_CONFIG_FILE"))

    def load(self) -> str | None:
        if self.path is None or not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get("base_url")
            return validate_url(value) if isinstance(value, str) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def save(self, value: str) -> str:
        value = validate_url(value)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".frigate-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"base_url": value}, handle)
                    handle.write("\n")
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return value
