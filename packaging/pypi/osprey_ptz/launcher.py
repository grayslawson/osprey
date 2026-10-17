"""Console entry point for the ``osprey`` command in the osprey-ptz wheel.

It puts the bundled controller modules on ``sys.path`` and calls the same
``main()`` the container and the source checkout use, so behaviour and flags
are identical across installation paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parent / "_app"


def main() -> int:
    # The controller uses flat imports (``import config``, ``from unifiwire
    # import certs``), so both bundled roots must be importable.
    sys.path[:0] = [str(_APP / "cuckoo"), str(_APP)]
    sys.argv[0] = "osprey"  # argparse prog name in --help
    from main import main as controller_main  # cuckoo/main.py

    return controller_main()
