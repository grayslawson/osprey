# Tasks: Installable Python Package and Single `osprey` CLI

**Input**: [plan.md](plan.md), [spec.md](spec.md). Not started. Sequence after the ptz-watchdog,
onvif-auth-profile-s, rtsp-hardening and web-ui branches merge, because they edit `cuckoo/`.

## Phase 1: Package move (no behaviour change)

- [ ] T001 Move `cuckoo/*.py` (excluding `tests/` and `harness/`) to `src/osprey/` with `git mv`
- [ ] T002 Convert flat imports to relative imports; keep `cuckoo/` as a one-release shim
- [ ] T003 Update `cuckoo/mypy.ini`, `cuckoo/test.sh`, and `.github/workflows/test.yml` paths
- [ ] T004 Confirm `make check` passes on the source tree with no change in test count

## Phase 2: CLI and runtime contract

- [ ] T005 Add `src/osprey/cli.py` with `run` as the default subcommand
- [ ] T006 Port `scripts/osprey-doctor.sh` checks to `osprey doctor` with the same prefixes and exit codes
- [ ] T007 Add `healthcheck`, `version`, `config validate`, `setup`, `manage`, `custody`, `cert`
- [ ] T008 Implement configuration precedence and `OSPREY_STATE_DIR` / `$STATE_DIRECTORY` resolution
- [ ] T009 Implement `*_FILE` secret lookup and `$CREDENTIALS_DIRECTORY`
- [ ] T010 Add unauthenticated `/healthz` and `/readyz`; tests for both

## Phase 3: Versioning and distribution

- [ ] T011 Switch to hatch-vcs; generate `src/osprey/_version.py`; expose the version in `/healthz`
- [ ] T012 Replace the transitional `osprey_ptz/_app` launcher with the real package; delete `packaging/pypi/`
- [ ] T013 Wheel-install test: run the suites against an installed wheel from a clean venv
- [ ] T014 Update the Dockerfile, Nix package and Proxmox install to use the wheel

## Phase 4: Verification

- [ ] T015 `systemd-analyze security` on the packaged unit scores 2.0 or lower
- [ ] T016 Update `docs/configuration.md`, `docs/nixos.md`, `docs/installation.md`, and `CHANGELOG.md`
