from __future__ import annotations

import pytest

import firmware


@pytest.mark.parametrize("value, expected", [
    ("5.3.95", "5.3.95"),
    ("UVC.SAV530q.v5.3.95.67.148b9a3.260612.0645", "5.3.95"),
    ("", None),
    ("0", None),
])
def test_release_of(value: str, expected: str | None) -> None:
    assert firmware.release_of(value) == expected


def test_status_tested_untested_unknown() -> None:
    assert firmware.firmware_status("UVC.SAV530q.v5.3.95.67.148b9a3.260612.0645")[0] == "tested"
    status, message = firmware.firmware_status("5.4.1")
    assert status == "untested" and "5.4.1" in message and "5.3.95" in message
    status, message = firmware.firmware_status("")
    assert status == "unknown" and "not reported" in message
