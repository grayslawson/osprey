"""Self-signed EC certificate for the control channel.

Neither end validates the other's certificate on this protocol — a connecting
camera's fingerprint is logged, not checked — so a self-signed pair is sufficient
at both ends. Uses openssl; any equivalent PEM dropped in place works as well.
"""

from __future__ import annotations

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
    subprocess.run(
        ["openssl", "ecparam", "-name", CURVE, "-genkey", "-noout", "-out", str(key)],
        check=True,
        capture_output=True,
    )
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
    path.write_bytes(cert.read_bytes() + key.read_bytes())
    key.unlink(missing_ok=True)
    cert.unlink(missing_ok=True)
    path.chmod(0o600)
    return path
