# Contributing to Osprey

Osprey is an integration layer for supported Ubiquiti PTZ cameras and an
existing Frigate installation. Contributions should keep that boundary clear.

## Development setup

Python 3.13 is required. The controller uses the standard library; the test
suite installs its own test-only dependencies. The wire library is tested from
the sibling `pyunifiwire/` directory.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pytest mypy paho-mqtt types-paho-mqtt
make check
```

No camera, Frigate instance, MQTT broker, or internet connection is needed for
the default checks. Use the harness scripts only when you explicitly have a
controlled test device and network.

## Pull requests

Include the user-visible outcome, affected interfaces, tests run, and any
residual risk. Configuration changes must include a neutral example and an
upgrade or rollback note. Never attach credentials, private addresses, MAC
addresses, recordings, or unredacted logs.

Before requesting review:

- run `make check`;
- review `git diff --check` and the list of changed files;
- update `docs/`, `README.md`, or release notes for user-facing behavior;
- verify Docker/Podman, Home Assistant, NixOS, and package metadata when those
  paths are affected.

Security reports should follow [`SECURITY.md`](SECURITY.md), not a public issue.
