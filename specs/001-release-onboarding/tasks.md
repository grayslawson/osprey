# Tasks: Release Onboarding and Diagnostics

**Input**: Design documents in `/specs/001-release-onboarding/`
**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`

## Phase 1: Setup

**Purpose**: Establish repeatable validation fixtures without changing production behavior.

- [X] T001 [P] Add isolated setup and doctor fixture helpers in `cuckoo/tests/tests_support.py`
- [X] T002 [P] Record the setup and doctor contract references in `cuckoo/tests/README.md`

## Phase 2: Foundational

**Purpose**: Create shared assertions for atomic writes, status prefixes, and secret redaction.

- [X] T003 [P] Add reusable secret-redaction assertions in `cuckoo/tests/tests_support.py`
- [X] T004 [P] Add a shell-test runner for `scripts/osprey-doctor.sh` in `cuckoo/tests/test_doctor.py`
- [X] T005 [P] Add contract fixtures for neutral setup payloads in `cuckoo/tests/test_setup_wizard.py`

**Checkpoint**: Shared validation fixtures are available before story work begins.

## Phase 3: User Story 1 - Complete First-Run Setup (Priority: P1)

**Goal**: A new operator can safely complete setup and receive durable generated artifacts.

**Independent Test**: Run `cuckoo/tests/test_setup_wizard.py` against a temporary state directory
and verify valid, invalid, repeated, and interrupted saves.

### Tests

- [X] T006 [P] [US1] Test valid setup creates runtime JSON, Frigate metadata, and mode-0600 secret files in `cuckoo/tests/test_setup_wizard.py`
- [X] T007 [P] [US1] Test invalid input and invalid setup tokens do not write partial state in `cuckoo/tests/test_setup_wizard.py`
- [X] T008 [P] [US1] Test completed setup rejects replay and preserves prior files in `cuckoo/tests/test_setup_wizard.py`

### Implementation and Documentation

- [X] T009 [US1] Harden setup save interruption and restart-state handling in `cuckoo/setup_wizard.py`
- [X] T010 [US1] Verify setup route status and completion behavior in `cuckoo/main.py`
- [X] T011 [P] [US1] Document first-run recovery, generated artifacts, and reset boundaries in `docs/onboarding-runbook.md` and `docs/installation.md`

**Checkpoint**: User Story 1 is independently testable and preserves secrets and prior state.

## Phase 4: User Story 2 - Diagnose a Deployment (Priority: P1)

**Goal**: One read-only command identifies local, routing, runtime, callback, and Frigate failures.

**Independent Test**: Run `cuckoo/tests/test_doctor.py` with one deliberate failure per check
and assert independent results and exit codes.

### Tests

- [X] T012 [P] [US2] Test healthy doctor output and zero exit status in `cuckoo/tests/test_doctor.py`
- [X] T013 [P] [US2] Test invalid host, config, route, port, runtime, and Frigate failures in `cuckoo/tests/test_doctor.py`
- [X] T014 [P] [US2] Test optional-tool warnings do not suppress unrelated checks in `cuckoo/tests/test_doctor.py`

### Implementation and Documentation

- [X] T015 [US2] Add explicit callback reachability input and validation while preserving read-only behavior in `scripts/osprey-doctor.sh`
- [X] T016 [US2] Ensure doctor diagnostics distinguish required errors from optional warnings and document remediation in `scripts/osprey-doctor.sh`
- [X] T017 [P] [US2] Document doctor usage for Docker, Podman, Home Assistant, NixOS, and package installs in `docs/installation.md`

**Checkpoint**: User Story 2 is independently testable with stable output and exit status.

## Phase 5: User Story 3 - Understand Protocol Exposure (Priority: P2)

**Goal**: Operators can choose and audit ONVIF and RTSP exposure without secret leakage.

**Independent Test**: Exercise anonymous and authenticated protocol fixtures and inspect status,
logs, and diagnostic output for mode names without credential values.

### Tests

- [X] T018 [P] [US3] Add ONVIF mode and credential-redaction assertions in `cuckoo/tests/test_onvif_auth.py`
- [X] T019 [P] [US3] Add RTSP mode and stale-challenge redaction assertions in `cuckoo/tests/test_rtsp.py`
- [X] T020 [P] [US3] Add status/log redaction coverage for generated credentials in `cuckoo/tests/test_setup_wizard.py`

### Implementation and Documentation

- [X] T021 [US3] Audit status and error paths for protocol credential leakage in `cuckoo/main.py`, `cuckoo/onvif.py`, and `cuckoo/rtsp.py`
- [X] T022 [P] [US3] Update authentication and security guidance with active modes, network boundaries, and Frigate compatibility in `docs/authentication.md` and `docs/security.md`

**Checkpoint**: User Story 3 is independently testable for both compatibility and protected modes.

## Phase 6: Polish and Cross-Cutting Validation

- [X] T023 [P] Run focused setup, doctor, ONVIF, RTSP, and config tests and record results in `specs/001-release-onboarding/quickstart.md`
- [X] T024 [P] Run mypy, shell syntax, compose, package, and Home Assistant validation for affected artifacts
- [X] T025 [P] Review public documentation and generated examples for personal infrastructure, Finch, or secret leakage
- [X] T026 Run the complete repository validation suite and resolve any regression before release

## Dependencies and Execution Order

### Phase Dependencies

- Phase 1 has no dependencies and may begin immediately.
- Phase 2 depends on Phase 1 and blocks story work.
- User Stories 1 and 2 depend only on Phase 2 and may proceed in parallel.
- User Story 3 depends on Phase 2 and may proceed in parallel with Stories 1 and 2.
- Phase 6 depends on the desired stories completing.

### Parallel Opportunities

- T001–T005 are parallel where they touch separate fixtures or assertions.
- T006–T008, T012–T014, and T018–T020 are parallel test-writing tasks.
- Documentation tasks T011, T017, and T022 are parallel when their files do not overlap.

## Implementation Strategy

1. Complete setup and foundational fixtures.
2. Deliver User Story 1 as the MVP and validate it independently.
3. Deliver User Story 2 and validate diagnostics independently.
4. Deliver User Story 3 and validate both authentication modes independently.
5. Run the complete cross-cutting validation and review public artifacts.
