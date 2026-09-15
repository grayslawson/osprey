# Osprey agent guide

This file is the first stop for coding agents and contributors. Keep it short,
executable, and current. The repository is the source of truth; do not rely on
unstated machine state, chat history, or private deployment details.

## Product boundary

Osprey controls supported Ubiquiti PTZ cameras and exposes ONVIF/RTSP services
for a separately deployed Frigate installation. Osprey does not install,
bundle, or reconfigure Frigate. The `cuckoo/` directory is the historical
Python package name; user-facing documentation and new code should call the
product Osprey.

Never commit camera credentials, tokens, private IP addresses, recordings,
generated certificates, or deployment state. Use the neutral examples already
present in the documentation and `.env.*.example` files.

## Repository map

- `cuckoo/`: controller, ONVIF, RTSP, admin, and integration code
- `pyunifiwire/`: shared UniFi camera wire-protocol library
- `docs/`: user and operator documentation
- `homeassistant/`, `nix/`, `packaging/`, `docker/`: distribution targets
- `specs/`: Spec Kit specifications, plans, contracts, and task lists
- `.github/workflows/`: release and validation gates

Read the relevant module and its tests before changing behavior. For protocol
changes, also read `docs/README.md`, `cuckoo/ARCHITECTURE.md`, and the applicable
integration guide.

## Fast feedback loop

Run the narrowest check first, then the full gate:

```sh
make test                  # unit and integration tests, no camera required
make check                 # tests, type checks, static repository checks
./scripts/osprey doctor    # runtime checks for a configured deployment
```

Useful focused commands:

```sh
python3 -m pytest -q cuckoo/tests/test_onvif.py
python3 -m pytest -q cuckoo/tests/test_setup_wizard.py
python3 -m pytest -q pyunifiwire/tests
python3 -m mypy --config-file cuckoo/mypy.ini cuckoo
sh homeassistant/validate.sh
```

Hardware and network acceptance tests are opt-in. Do not make the default test
path require a camera, Frigate, MQTT broker, Docker daemon, or internet access.

## Change protocol

1. State the user outcome and affected boundary.
2. For feature work, update the applicable Spec Kit artifacts under `specs/`:
   spec, plan, tasks, and verification notes.
3. Add tests for success, malformed input, timeout/loss of connectivity,
   cancellation, and recovery wherever the boundary supports them.
4. Keep changes small and avoid unrelated rewrites. Preserve public contracts
   unless a breaking change is explicitly documented.
5. Update user documentation and release notes when behavior or configuration
   changes.
6. Run `make check`, inspect the diff, and report any residual risk.

## Safety and security

- Validate and bound every PTZ, zoom, preset, MQTT, browser, and discovery input.
- Treat camera, Frigate, MQTT, and callback networks as untrusted boundaries.
- Do not weaken authentication or expose a new listener without documenting the
  trust boundary and compatibility impact.
- Logs and test fixtures must not contain secrets or unnecessary personal data.
- Prefer deterministic tests and fake peers over sleeps or live services.

## Definition of done

A change is complete only when its acceptance behavior is tested, supported
installation paths remain valid, documentation is updated, `make check` passes,
and any known compatibility or security limitation is written down.
