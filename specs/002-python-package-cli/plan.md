# Implementation Plan: Installable Python Package and Single `osprey` CLI

**Spec**: [spec.md](spec.md) | **Branch**: `002-python-package-cli` (planned)

## Summary

Move `cuckoo/*.py` into `src/osprey/` as a real package with relative imports, add one `osprey`
console entry point with the subcommands in spec User Story 2, derive the version from tags with
hatch-vcs, and replace the transitional `osprey_ptz/_app` launcher in the root `pyproject.toml`.
The change is mechanical for the controller and must not change protocol behaviour.

## Technical Context

- **Language**: Python 3.13, standard library at runtime (`paho-mqtt` and `uiprotect` as extras).
- **Build**: hatchling with hatch-vcs (`version_scheme = "guess-next-dev"`,
  `local_scheme = "no-local-version"`), writing `src/osprey/_version.py`.
- **Tests**: existing `cuckoo/tests` and `pyunifiwire/tests`, plus wheel-install tests that run
  the same suites against an installed wheel in a clean virtual environment.
- **Lint and types**: ruff (`ruff.toml`), mypy (`cuckoo/mypy.ini`, `pyunifiwire`).
- **Packaging consumers**: OCI image (`docker/Dockerfile`), NixOS (`package.nix`, `nix/osprey.nix`),
  Proxmox (`packaging/proxmox`), Home Assistant (`homeassistant/addon`).

## Current state (2026-10-17)

| Area | State |
|---|---|
| Source layout | Flat modules in `cuckoo/`, imported via `PYTHONPATH`; `cuckoo/` is also the test root. |
| Wheel | Transitional: root `pyproject.toml` bundles `cuckoo/` and `pyunifiwire/src/unifiwire` under `osprey_ptz/_app/`, with `packaging/pypi/osprey_ptz/launcher.py` as the entry point. |
| Version | `VERSION` file; checked by `scripts/validate-repo.sh`. |
| Nix | Package builds with `makeWrapper` (openssl and ffmpeg on PATH, paho-mqtt in the Python env). |
| Ruff | Enabled for every path except `cuckoo/` (17 findings, see `ruff.toml`). |

## Steps

1. **Move the package.** `git mv cuckoo/*.py src/osprey/` (excluding `tests/` and `harness/`),
   rewrite flat imports as relative imports, keep `cuckoo/` as a compatibility shim for one
   release. Update `cuckoo/mypy.ini` and the test runner to the new paths.
2. **Entry point.** Add `src/osprey/cli.py` with `run`, `setup`, `doctor`, `healthcheck`,
   `manage`, `custody`, `cert`, `config validate`, `version`. `run` is the default. The shell
   `doctor` is ported check for check and keeps its output prefixes.
3. **Runtime contract.** Implement the precedence and path rules in spec FR-004, the `*_FILE` and
   `$CREDENTIALS_DIRECTORY` secret lookup, and `$STATE_DIRECTORY` handling. Document the contract
   in `docs/configuration.md`.
4. **Health endpoints.** Add unauthenticated `/healthz` and `/readyz` on the control listener and
   wire `osprey healthcheck` to them.
5. **Version.** Switch to hatch-vcs, generate `src/osprey/_version.py`, and remove the `VERSION`
   file and its checks in `scripts/validate-repo.sh` once tags are the only source.
6. **Packaging cleanup.** Delete `osprey_ptz/_app` and `packaging/pypi`; point the Nix package,
   Dockerfile and Proxmox install at the wheel.
7. **Verification.** Build the wheel, install it in a clean venv, run the suites against the
   installed package, run `make check`, and run `systemd-analyze security` on the packaged unit.

## Risks

- Import-path changes can hide failures that only appear when the package is installed (for
  example a module that relied on the current directory). Mitigation: the wheel-install test in
  step 7 is required before the transitional launcher is removed.
- `pyunifiwire` is bundled, so a protocol fix needs an Osprey release. Accepted by the owner.
- Other branches edit `cuckoo/` concurrently (ptz-watchdog, onvif, rtsp, web-ui). The move must
  land after those branches merge, or be rebased mechanically with `git mv` detection.

## Not in this plan

Protocol changes, the PTZ state machine, and web UI redesign are owned by their own branches.
