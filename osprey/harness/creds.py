"""Credentials for the ONVIF harnesses.

Both walks connect with `ONVIFCamera(host, port, user, pass)`. With an empty
user the client is anonymous, which is all a walk needed while Osprey answered
anonymously. Once Osprey requires a WS-Security UsernameToken the walk has to
send one, so the credentials come from the environment:

    OSPREY_ONVIF_USERNAME=frigate OSPREY_ONVIF_PASSWORD=... python3 harness/ha_walk.py ...

The same names Osprey itself reads, so the same secret file works for both
sides. Nothing is printed: a walk is a black-box test, not a credential check.
"""

from __future__ import annotations

import os


def onvif_credentials() -> tuple[str, str]:
    """The username and password for the ONVIF walk, empty when none are set."""
    return (
        os.environ.get("OSPREY_ONVIF_USERNAME", "").strip(),
        os.environ.get("OSPREY_ONVIF_PASSWORD", ""),
    )
