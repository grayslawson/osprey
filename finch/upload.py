"""Uploading a snapshot to wherever the controller says.

The controller does not fetch snapshots; it sends a one-time HTTPS URL and expects
the camera to POST an image to it. So this is a client, not a server — the mirror
of what cuckoo has to serve.

The image is whatever the caller hands over. finch does not render anything: if a
source frame is available it is turned into a JPEG with ffmpeg, and if not, a
small valid placeholder goes out instead, because a controller that gets nothing
retries forever.
"""

from __future__ import annotations

import logging
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

UPLOAD_TIMEOUT_SEC: Final = 15.0

log = logging.getLogger("finch.upload")

# A 1×1 grey JPEG. Not a picture of anything, but a valid image, which is what
# matters when the alternative is an empty body.
PLACEHOLDER_JPEG: Final = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffc00011080001000101011100ffc400"
    "1f0000010501010101010100000000000000000102030405060708090a0bffc400b51000"
    "02010303020403050504040000017d01020300041105122131410613516107227114328191"
    "a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a4344"
    "45464748494a535455565758595a636465666768696a737475767778797a8384858687888"
    "98a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9"
    "cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100"
    "3f00fb47ffd9"
)


def still_from(source: Path, at_seconds: float = 0.0) -> bytes:
    """One frame of the source video as a JPEG, if ffmpeg is available."""
    if shutil.which("ffmpeg") is None or not source.exists():
        return PLACEHOLDER_JPEG
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{at_seconds:.2f}", "-i", str(source),
                "-frames:v", "1", "-f", "mjpeg", "-",
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("could not render a still: %s", exc)
        return PLACEHOLDER_JPEG
    return result.stdout if result.stdout.startswith(b"\xff\xd8") else PLACEHOLDER_JPEG


def send(url: str, image: bytes) -> bool:
    """POST the image to the one-time URL. Returns whether it was accepted."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        url,
        data=image,
        headers={"Content-Type": "image/jpeg", "Content-Length": str(len(image))},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_SEC, context=context) as reply:
            accepted = reply.status in (200, 201, 204)
    except urllib.error.HTTPError as exc:
        log.warning("snapshot refused with %s", exc.code)
        return False
    except (OSError, ValueError) as exc:
        log.warning("snapshot upload failed: %s", exc)
        return False
    log.info("snapshot uploaded, %d bytes", len(image))
    return accepted
