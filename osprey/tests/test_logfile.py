"""Tests for the rotating on-disk application log."""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

import pytest

import logfile
from tests_support import assert_no_secret


def _detach(handler: logging.Handler | None) -> None:
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()


def _logger(name: str = "osprey.test") -> logging.Logger:
    """Return a logger that actually emits INFO records.

    The root logger defaults to WARNING, so an INFO record would be filtered
    before it reached the file handler.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    return logger


def test_install_writes_messages_to_the_state_directory(tmp_path: Path) -> None:
    handler = logfile.install(tmp_path)
    try:
        assert handler is not None
        _logger().info("camera ready: %s", "abc")
        handler.flush()
        log = tmp_path / "logs" / logfile.LOG_FILE_NAME
        assert log.is_file()
        assert "camera ready: abc" in log.read_text(encoding="utf-8")
    finally:
        _detach(handler)


def test_install_creates_the_log_directory(tmp_path: Path) -> None:
    handler = logfile.install(tmp_path)
    try:
        assert handler is not None
        assert (tmp_path / "logs").is_dir()
    finally:
        _detach(handler)


def test_install_is_idempotent(tmp_path: Path) -> None:
    first = logfile.install(tmp_path)
    try:
        second = logfile.install(tmp_path)
        assert second is not None
        handlers = [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logfile.RedactingFileHandler)
        ]
        assert len(handlers) == 1
    finally:
        _detach(first)
        _detach(second)


def test_secrets_are_redacted_in_the_persisted_log(tmp_path: Path) -> None:
    handler = logfile.install(tmp_path)
    try:
        assert handler is not None
        _logger().warning(
            "config rejected password=%s", "hunter2"
        )
        handler.flush()
        text = (tmp_path / "logs" / logfile.LOG_FILE_NAME).read_text(
            encoding="utf-8"
        )
        assert_no_secret(text, "hunter2")
        assert "[redacted]" in text
    finally:
        _detach(handler)


def test_uri_credentials_are_redacted(tmp_path: Path) -> None:
    handler = logfile.install(tmp_path)
    try:
        assert handler is not None
        _logger().info(
            "connecting to rtsp://user:sekrit@192.0.2.10:8554/video1"
        )
        handler.flush()
        text = (tmp_path / "logs" / logfile.LOG_FILE_NAME).read_text(
            encoding="utf-8"
        )
        assert_no_secret(text, "sekrit")
    finally:
        _detach(handler)


def test_the_original_record_is_not_mutated(tmp_path: Path) -> None:
    handler = logfile.install(tmp_path)
    try:
        assert handler is not None
        logger = _logger()
        record = logger.makeRecord(
            "osprey.test", logging.INFO, __file__, 1, "password=%s", ("swordfish",),
            None,
        )
        handler.handle(record)
        handler.flush()
        assert record.msg == "password=%s"
        assert record.args == ("swordfish",)
        text = (tmp_path / "logs" / logfile.LOG_FILE_NAME).read_text(
            encoding="utf-8"
        )
        assert_no_secret(text, "swordfish")
    finally:
        _detach(handler)


def test_the_log_rotates(tmp_path: Path) -> None:
    handler = logfile.install(tmp_path, max_bytes=512, backup_count=2)
    try:
        assert handler is not None
        logger = _logger()
        for index in range(200):
            logger.info("filling the log file with a long line %d", index)
        handler.flush()
        log = tmp_path / "logs" / logfile.LOG_FILE_NAME
        assert log.is_file()
        assert log.stat().st_size <= 1024
        rotated = sorted(
            p for p in (tmp_path / "logs").iterdir() if p.name != log.name
        )
        assert rotated, "expected at least one rotated log file"
        assert len(rotated) <= 2
    finally:
        _detach(handler)


def test_a_read_only_state_directory_disables_file_logging(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o500)
    try:
        handler = logfile.install(state)
        assert handler is None
    finally:
        state.chmod(0o700)


def test_install_returns_a_handler_when_the_directory_is_unwritable(
    tmp_path: Path,
) -> None:
    """A missing parent is created rather than crashing the controller."""
    nested = tmp_path / "deep" / "state"
    handler = logfile.install(nested)
    try:
        assert handler is not None
        assert nested.is_dir()
    finally:
        _detach(handler)


def test_default_log_dir_is_inside_the_state_directory(tmp_path: Path) -> None:
    assert logfile.default_log_dir(tmp_path) == tmp_path / "logs"
