"""The generated control key is never group/world readable."""

from __future__ import annotations

import os
from pathlib import Path

from unifiwire import certs


def test_pem_is_created_0600_even_with_a_permissive_umask(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        path = certs.ensure(tmp_path / "osprey.pem")
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.key")) and not list(tmp_path.glob("*.crt"))
