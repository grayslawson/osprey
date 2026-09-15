# Implementation Plan: Release Onboarding and Diagnostics

**Branch**: `001-release-onboarding`
**Date**: 2026-09-15
**Spec**: [spec.md](spec.md)

## Summary

This feature makes a public Osprey installation self-describing and recoverable. The
existing first-run wizard validates operator input and atomically creates non-secret runtime
configuration plus protected secrets. The existing `osprey doctor` command performs read-only
environment checks. This plan verifies and closes the contract between those surfaces,
documentation, and protocol authentication behavior without making Frigate part of Osprey.

## Technical Context

**Language/Version**: Python 3.11+ application code; POSIX shell diagnostics
**Primary Dependencies**: Python standard library, existing Osprey ONVIF/RTSP/config modules,
`curl`, `ip`, `ss`, Docker Compose or Podman Compose when available
**Storage**: JSON runtime configuration and mode-0600 environment/password files
**Testing**: pytest, mypy, shell syntax checks, container/compose validation
**Target Platform**: Linux hosts and containers; Home Assistant and NixOS packaging consume the
same runtime contracts
**Project Type**: Network service with browser setup flow, CLI diagnostics, and protocol adapters
**Performance Goals**: A local doctor run completes within 10 seconds; setup writes are atomic
**Constraints**: Frigate remains external; ONVIF anonymous mode remains available for
interoperability; secrets must never appear in logs or public artifacts
**Scale/Scope**: One controller may serve multiple cameras; checks are bounded to configured
listeners, camera route, Frigate URL, and local runtime

## Constitution Check

*Gate: PASS before research.*

- **Safe Camera Control and Compatibility**: The feature does not change PTZ motion and keeps
  external Frigate ownership and anonymous-ONVIF compatibility explicit.
- **Secure by Explicit Trust Boundaries**: Setup writes protected files atomically, authentication
  modes are disclosed without secrets, and diagnostics remain read-only.
- **Testable and Observable Behavior**: Existing setup, authentication, and diagnostics tests are
  extended or verified for malformed, unreachable, interrupted, and redaction paths.
- **Operable Across Supported Installations**: The same contracts are documented for Docker,
  Podman, Home Assistant, NixOS, and package/script installs.
- **Small, Reviewable, User-Value-Driven Changes**: Scope is limited to onboarding, diagnostics,
  protocol exposure guidance, and their tests/documentation.

## Phase 0: Research Decisions

See [research.md](research.md). Repository inspection resolves the implementation questions;
no open technical clarification remains.

## Phase 1: Design

- [data-model.md](data-model.md) defines setup artifacts, diagnostic results, and protocol
  exposure records.
- [contracts/](contracts/) defines the setup HTTP contract and doctor command contract.
- [quickstart.md](quickstart.md) provides runnable validation scenarios.

## Project Structure

```text
specs/001-release-onboarding/
├── plan.md
├── research.md
├── data-model.md
├── contracts/
│   ├── setup-http.md
│   └── doctor-cli.md
├── quickstart.md
└── tasks.md

cuckoo/
├── setup_wizard.py
├── main.py
├── config.py
├── onvif.py
├── rtsp.py
└── tests/
    ├── test_setup_wizard.py
    ├── test_onvif_auth.py
    ├── test_rtsp.py
    └── test_config.py

scripts/
└── osprey-doctor.sh

docs/
├── installation.md
├── security.md
├── authentication.md
└── onboarding-runbook.md
```

**Structure Decision**: Reuse the existing service, shell diagnostic, test, and documentation
boundaries. No new runtime service or Frigate dependency is introduced.

## Constitution Check: Post-Design

*Gate: PASS.* The design preserves external Frigate ownership, keeps media and ONVIF
authentication independent, uses atomic secret writes and read-only diagnostics, and identifies
test and documentation touch points for every functional requirement.

## Complexity Tracking

No constitution violations or additional architectural complexity require justification.
