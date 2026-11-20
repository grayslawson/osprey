"""Rotating on-disk application log files for the persistent state directory.

The operator console shows a bounded in-memory tail, which disappears when the
process restarts.  This module adds a rotating file handler so a deployment
keeps a short history of what happened across restarts, in the same state
directory that already holds the certificate and presets.

Design constraints:

- Logs must never take the controller down.  Every failure path here degrades
  to "no file logging" rather than raising into the caller's log call.
- The handler redacts messages with the same rules as the console buffer, so a
  credential that reaches a log line is not persisted verbatim.
- The state directory may be read-only or missing in some deployments.  The
  caller decides whether to install the handler at all.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Final

import logbuffer

DEFAULT_MAX_BYTES: Final = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT: Final = 3
LOG_FILE_NAME: Final = "osprey.log"


def default_log_dir(state_dir: str | os.PathLike[str]) -> Path:
    """Return the directory that holds Osprey's persistent logs."""
    return Path(state_dir) / "logs"


def _redacting_formatter() -> logging.Formatter:
    """Format records with the same redaction the console buffer applies."""
    return logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")


class RedactingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating file handler that redacts the message it writes.

    ``RotatingFileHandler`` formats the record itself, so overriding ``format``
    is the single place to apply redaction to the persisted copy.
    """

    def format(self, record: logging.LogRecord) -> str:
        original = record.msg
        original_args = record.args
        try:
            record.msg = logbuffer.redact(record.getMessage())
            record.args = ()
            return super().format(record)
        finally:
            record.msg = original
            record.args = original_args


def install(
    state_dir: str | os.PathLike[str],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    level: int = logging.INFO,
) -> logging.Handler | None:
    """Attach a rotating file handler for the state directory.

    Returns the handler, or ``None`` when the state directory is not usable.
    The caller keeps running with console logging only in that case; a
    read-only state volume must not stop the controller from starting.

    Calling this twice for the same state directory reuses the handler that is
    already attached instead of opening a second file.
    """
    directory = default_log_dir(state_dir)
    path = directory / LOG_FILE_NAME
    for existing in logging.getLogger().handlers:
        if isinstance(existing, RedactingFileHandler) and _same_path(existing, path):
            return existing

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    try:
        handler = RedactingFileHandler(
            path,
            maxBytes=max(1, int(max_bytes)),
            backupCount=max(0, int(backup_count)),
            encoding="utf-8",
            delay=True,
        )
    except OSError:
        return None

    handler.setLevel(level)
    handler.setFormatter(_redacting_formatter())
    root = logging.getLogger()
    root.addHandler(handler)
    return handler


def _same_path(handler: logging.Handler, path: Path) -> bool:
    """True when the handler already writes to ``path``."""
    base = getattr(handler, "baseFilename", None)
    if not base:
        return False
    try:
        return Path(base) == path
    except OSError:  # pragma: no cover - defensive
        return False
