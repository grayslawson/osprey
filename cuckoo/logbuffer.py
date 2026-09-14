"""Bounded in-memory application log records for the operator console.

The console is intentionally not a replacement for the deployment's journal or
container logs.  This small ring buffer gives an operator enough recent context
to diagnose a disconnected camera without reading from the host filesystem.
Records are copied after formatting, redacted, and capped so a noisy client
cannot grow Osprey's memory indefinitely.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Final

DEFAULT_LIMIT: Final = 500
MAX_MESSAGE_BYTES: Final = 4096

# Do not expose common credential-bearing fields in the browser response.  The
# application should never log these values, but defence in depth matters when
# a dependency includes a request URL or exception text in a log message.
_SECRET = re.compile(
    r"(?i)\b(password|passwd|secret|token|authorization|cookie)\b"
    r"(\s*[:=]\s*)([\"']?)([^\"'\s,;}\]]+)(\3)"
)
_URI_CREDENTIALS = re.compile(r"(?i)(://[^/\s:@]+:)[^@\s]+(@)")


def redact(message: str) -> str:
    """Remove values following credential-like keys and bound the result."""
    safe = _SECRET.sub(r"\1\2\3[redacted]\3", message)
    safe = _URI_CREDENTIALS.sub(r"\1[redacted]\2", safe)
    return safe.encode("utf-8", "replace")[:MAX_MESSAGE_BYTES].decode(
        "utf-8", "ignore"
    )


class LogBuffer(logging.Handler):
    """A thread-safe ring handler suitable for a read-only diagnostics API."""

    def __init__(self, max_records: int = DEFAULT_LIMIT) -> None:
        super().__init__(level=logging.INFO)
        self._records: deque[dict[str, object]] = deque(maxlen=max_records)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact(record.getMessage())
            if record.exc_info:
                # Include only the exception type/message; full tracebacks belong
                # in the deployment journal and may contain sensitive paths.
                exc = record.exc_info[1]
                if exc is not None:
                    message = redact(f"{message}: {type(exc).__name__}: {exc}")
            entry = {
                "timestamp": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "epoch": round(record.created, 3),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
            with self._lock:
                self._records.append(entry)
        except Exception:  # pragma: no cover - logging must never break callers
            self.handleError(record)

    def entries(self, limit: int = 100, minimum: int = logging.INFO) -> list[dict[str, object]]:
        """Return newest records first, optionally filtering by severity."""
        limit = max(1, min(limit, DEFAULT_LIMIT))
        with self._lock:
            records = [
                dict(entry)
                for entry in self._records
                if logging._nameToLevel.get(str(entry["level"]), logging.INFO)
                >= minimum
            ]
        return list(reversed(records[-limit:]))

    def summary(self) -> dict[str, int]:
        """Return counts useful for an alert badge without exposing log text."""
        with self._lock:
            warning = sum(
                logging._nameToLevel.get(str(e["level"]), logging.INFO)
                >= logging.WARNING
                for e in self._records
            )
            errors = sum(
                logging._nameToLevel.get(str(e["level"]), logging.INFO)
                >= logging.ERROR
                for e in self._records
            )
        return {"warnings": warning, "errors": errors}


BUFFER = LogBuffer()


def install() -> LogBuffer:
    """Attach the shared buffer once and return it for dependency injection."""
    root = logging.getLogger()
    if BUFFER not in root.handlers:
        root.addHandler(BUFFER)
    return BUFFER
