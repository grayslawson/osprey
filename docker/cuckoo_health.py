"""Container health check: the ONVIF/operator listener accepts TCP.

This is a liveness probe only. It does not require a camera, so a freshly
started container without an adopted camera reports healthy. A dedicated
``/healthz`` endpoint is planned (see specs/002-python-package-cli).

The probe target follows the controller's own settings:

* ``OSPREY_HEALTH_HOST`` if set, else ``OSPREY_BIND`` unless it is a wildcard,
  else ``127.0.0.1``;
* ``OSPREY_HEALTH_PORT`` if set, else ``ports.onvif`` from the config file
  (``OSPREY_CONFIG_FILE``), else 8000.
"""

from __future__ import annotations

import json
import os
import socket
import sys

DEFAULT_PORT = 8000
WILDCARDS = {"", "0.0.0.0", "::", "[::]"}


def target() -> tuple[str, int]:
    host = os.environ.get("OSPREY_HEALTH_HOST", "").strip()
    if not host:
        bind = os.environ.get("OSPREY_BIND", "").strip()
        host = "127.0.0.1" if bind in WILDCARDS else bind.strip("[]")
    port_text = os.environ.get("OSPREY_HEALTH_PORT", "").strip()
    port = DEFAULT_PORT
    if port_text:
        port = int(port_text)
    else:
        path = os.environ.get("OSPREY_CONFIG_FILE", "")
        try:
            with open(path, encoding="utf-8") as handle:
                configured = json.load(handle).get("ports", {}).get("onvif")
            if isinstance(configured, int):
                port = configured
        except (OSError, ValueError, AttributeError):
            pass
    if not 0 < port < 65536:
        raise ValueError(f"invalid health check port: {port}")
    return host, port


def main() -> int:
    try:
        host, port = target()
        with socket.create_connection((host, port), timeout=2.0):
            pass
    except (OSError, ValueError) as exc:
        print(f"osprey healthcheck failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
