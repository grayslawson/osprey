"""Container health check: ``GET /healthz`` on the ONVIF/operator listener.

Healthy only on HTTP 200. Osprey answers 503 while it waits for a camera and
when the camera, PTZ channel or video is degraded, so all of those mark the
container unhealthy.

The probe target follows the controller's own settings:

* ``OSPREY_HEALTH_HOST`` if set, else ``OSPREY_BIND`` unless it is a wildcard,
  else ``127.0.0.1``;
* ``OSPREY_HEALTH_PORT`` if set, else ``ports.onvif`` from the config file
  (``OSPREY_CONFIG_FILE``), else 8000.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

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
        url = f"http://{f'[{host}]' if ':' in host else host}:{port}/healthz"
        with urllib.request.urlopen(url, timeout=2.0) as reply:
            if reply.status != 200:
                raise OSError(f"/healthz returned {reply.status}")
    except (OSError, ValueError) as exc:  # URLError and HTTPError (non-2xx) are OSErrors
        print(f"osprey healthcheck failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
