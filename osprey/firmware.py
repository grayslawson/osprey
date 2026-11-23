"""Camera firmware versions Osprey has been tested against.

The camera's private protocol can change with any firmware update (see NOTICE),
so an unlisted firmware is allowed but flagged in the logs and the status API.
Add a version here only after exercising it on a real camera.
"""

from __future__ import annotations

import re
from typing import Final

# Release triple (major.minor.patch) as reported in the hello "fwVersion", which
# is either "5.3.95" or the long form "UVC.SAV530q.v5.3.95.67.148b9a3.260612.0645".
TESTED_FIRMWARE: Final[tuple[str, ...]] = ("5.3.95",)

_RELEASE = re.compile(r"(?:^|[^0-9A-Za-z])v?(\d+\.\d+\.\d+)(?![0-9])")


def release_of(firmware: str) -> str | None:
    """The ``major.minor.patch`` release inside a camera firmware string."""
    match = _RELEASE.search(firmware or "")
    return match.group(1) if match else None


def firmware_status(firmware: str) -> tuple[str, str]:
    """Return ``(status, message)``; status is ``tested``, ``untested`` or ``unknown``."""
    tested = ", ".join(TESTED_FIRMWARE)
    if not firmware or release_of(firmware) is None:
        return "unknown", (
            f"Camera firmware is unknown ({firmware or 'not reported'}). "
            f"Osprey is tested with firmware {tested}; behaviour on other versions is unverified."
        )
    if release_of(firmware) in TESTED_FIRMWARE:
        return "tested", f"Firmware {firmware} is a tested version."
    return "untested", (
        f"Firmware {firmware} has not been tested with Osprey (tested: {tested}). "
        "PTZ or streaming may misbehave; consider not updating the camera until a release lists it."
    )
