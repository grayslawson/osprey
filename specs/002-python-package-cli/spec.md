# Feature Specification: Installable Python Package and Single `osprey` CLI

**Feature Branch**: `002-python-package-cli` (planned)
**Created**: 2026-10-17
**Status**: Draft (Phase 0 of the packaging plan)
**Input**: Make Osprey installable as one Python distribution with one `osprey` command,
so that every packaging path (PyPI, OCI image, NixOS, Debian/RPM, Proxmox LXC, Home Assistant)
installs the same code and the same version.

## Background

Today the controller is a set of flat modules under `cuckoo/` (`config`, `media`, `events`,
`model`, ...) that run as `python3 cuckoo/main.py` with `PYTHONPATH` set by the caller. The
wire library lives in `pyunifiwire/` as a separate project. Flat top-level names cannot be
installed into `site-packages` without colliding, so no packaging path can produce a clean
install today. The release-era transitional wheel (`osprey-ptz`, built from the root
`pyproject.toml`) bundles these modules under `osprey_ptz/_app/` as a stopgap; this feature
replaces it.

## User Scenarios & Testing

### User Story 1 - Install and run from any directory (Priority: P1)

As an operator, I can install Osprey with one command and start it from any working directory,
so that state and certificates do not depend on where I happen to run it.

**Independent Test**: In a clean virtual environment, `pip install osprey-ptz` (or
`uv tool install osprey-ptz`), then `cd /` and `osprey run --host 192.0.2.10` starts the
listeners and writes the certificate under the state directory.

**Acceptance Scenarios**:

1. **Given** a clean environment, **When** the wheel is installed, **Then** `osprey version`
   prints the same version as the release tag.
2. **Given** `OSPREY_STATE_DIR` is unset and `$STATE_DIRECTORY` is set by systemd, **When**
   `osprey run` starts, **Then** the certificate and wizard overlay are written under that
   directory and nowhere else.
3. **Given** no state directory is writable, **When** `osprey run` starts, **Then** it exits
   with a single actionable error before opening any socket.

### User Story 2 - Operate the controller with one command (Priority: P1)

As an operator or a container entrypoint, I use `osprey` subcommands for every routine task,
so that the shell launchers and `main.py` invocations are no longer needed.

**Acceptance Scenarios**:

1. `osprey run` starts the controller (the default subcommand).
2. `osprey setup` runs the first-run wizard without starting the controller.
3. `osprey doctor` ports the read-only shell diagnostics to Python with the same check
   names, the same OK/WARNING/ERROR semantics, and the same exit codes (`1` only for a required
   error).
4. `osprey healthcheck` exits `0` when the ONVIF/console listener accepts TCP, and non-zero
   otherwise (used by Docker `HEALTHCHECK`, systemd and Home Assistant watchdogs).
5. `osprey config validate [--config PATH]` validates a configuration file without opening
   sockets.
6. `osprey manage`, `osprey custody` and `osprey cert` keep their current behaviour behind the
   same single entry point.

### User Story 3 - One version everywhere (Priority: P2)

As a maintainer, I change the version in one place (a Git tag), and the wheel, the image label,
`/healthz`, the web UI and `osprey version` all report that version.

**Acceptance Scenarios**:

1. A tagged build sets the package version from the tag through hatch-vcs and writes
   `src/osprey/_version.py`. Until that lands, the root `VERSION` file is the source and
   `scripts/validate-repo.sh` fails when dependent files disagree with it.
2. An untagged build reports a PEP 440 dev version with no local segment (PyPI rejects local
   versions).
3. `CONTROLLER_VERSION` (the protocol version) stays separate from the package version.

### User Story 4 - Secrets and health without leaking them (Priority: P2)

As an operator on a NAS or in a container, I provide secrets as files or via the systemd
credentials directory, and I can probe liveness and readiness without credentials.

**Acceptance Scenarios**:

1. Every `OSPREY_*_PASSWORD` value has an `OSPREY_*_PASSWORD_FILE` form, and `$CREDENTIALS_DIRECTORY`
   is searched for well-known credential names.
2. `/healthz` (liveness) and `/readyz` (listeners bound, certificate loaded, camera connected)
   are unauthenticated and return no secret material.

## Requirements

- **FR-001**: The distribution name is `osprey-ptz`. The import package is `osprey`, under
  `src/osprey/`. The import names `config`, `media`, `events` and `model` no longer appear at the
  top level of site-packages.
- **FR-002**: `pyunifiwire` is bundled into the wheel (not published separately). Its upstream
  metadata is dropped; its LICENSE is kept and shipped with the wheel.
- **FR-003**: Runtime dependencies are standard library only. `paho-mqtt` is the `mqtt` extra and
  `uiprotect` is the `custody` extra.
- **FR-004**: Configuration precedence is: defaults < `/etc/osprey/osprey.json` (may be read-only)
  < `$STATE_DIR/osprey.json` (written by the wizard) < `OSPREY_*` environment < CLI flags.
- **FR-005**: `openssl` is a hard runtime dependency for certificate generation. `ffmpeg` is
  recommended and only needed for the operator preview.
- **FR-006**: The camera-facing listeners (TCP 7442, 7550, 7444) use the configured LAN address.
  `osprey doctor` reports an error when `host` is a wildcard or a container bridge address.
- **FR-007**: Only `run`, `setup`, `manage` and `custody` may change camera, Frigate or Protect
  state, and each does so only when invoked explicitly. `doctor`, `healthcheck`, `version` and
  `config validate` are read-only.
- **FR-008**: Existing `OSPREY_*` variables, the `--config` flag and `cuckoo.json` upgrade
  handling keep working for at least one minor release after the switch, with a warning.

## Out of Scope

- Changing protocol behaviour, the PTZ state machine, or the ONVIF/RTSP implementations (those
  belong to the ptz-watchdog, onvif and rtsp branches).
- The web UI rewrite (web-ui branch) beyond the `/healthz` and `/readyz` endpoints.
- Publishing `pyunifiwire` as its own distribution.

## Owner decisions (recorded)

- PyPI distribution name: `osprey-ptz`.
- `pyunifiwire`: bundled inside the wheel; upstream rjmotion metadata dropped; license kept.

## Success Criteria

- **SC-001**: A clean `pip install` of the built wheel runs `osprey --help`, `osprey version` and
  `osprey run` from `/` without `PYTHONPATH`.
- **SC-002**: The existing test suites (`cuckoo/tests`, `pyunifiwire/tests`) pass against the
  installed wheel, not only against the source tree.
- **SC-003**: `systemd-analyze security osprey` scores 2.0 or lower with the packaged unit.
- **SC-004**: Each release version is written in one place (the tag, or `VERSION` until hatch-vcs
  lands). Every other file that repeats it is checked by CI.
