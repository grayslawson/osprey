"""Self-signed EC certificate for the control channel.

Neither end validates the other's certificate on this protocol — a connecting
camera's fingerprint is logged, not checked — so a self-signed pair is sufficient
at both ends. Uses openssl; any equivalent PEM dropped in place works as well.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

CURVE: Final = "prime256v1"
DAYS: Final = 3650


def ensure(path: Path, common_name: str = "unifi-device") -> Path:
    """Create a combined key+cert PEM at `path` if it is not already there."""
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    key = path.with_suffix(".key")
    cert = path.with_suffix(".crt")
    old_umask = os.umask(0o077)  # openssl writes the bare key with the process umask
    try:
        subprocess.run(
            ["openssl", "ecparam", "-name", CURVE, "-genkey", "-noout", "-out", str(key)],
            check=True,
            capture_output=True,
        )
    finally:
        os.umask(old_umask)
    subprocess.run(
        [
            "openssl", "req", "-new", "-x509",
            "-key", str(key),
            "-out", str(cert),
            "-days", str(DAYS),
            "-subj", f"/CN={common_name}",
        ],
        check=True,
        capture_output=True,
    )
    # Created 0600 from the start, never readable by others.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(cert.read_bytes() + key.read_bytes())
    os.chmod(path, 0o600)
    key.unlink(missing_ok=True)
    cert.unlink(missing_ok=True)
    return path
