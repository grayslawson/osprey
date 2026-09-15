# Feature Specification: Release Onboarding and Diagnostics

**Feature Branch**: `001-release-onboarding`
**Created**: 2026-09-15
**Status**: Draft
**Input**: Harden first-run onboarding and operational diagnostics for public Osprey releases.

## User Scenarios & Testing

### User Story 1 - Complete first-run setup (Priority: P1)

As a new operator, I can open Osprey in a browser, provide the address it should bind to,
identify a camera, point it at my existing Frigate installation, and choose an administrator
password so that Osprey is usable without hand-editing configuration files.

**Why priority**: A reliable first run is the shortest path from installation to a working
camera integration and prevents unsafe, undocumented defaults.

**Independent Test**: Start with an empty persistent data directory, complete the wizard with
valid values, restart Osprey, and verify that the configured service and camera are available.

**Acceptance Scenarios**:

1. **Given** an unconfigured Osprey instance, **When** the operator submits valid host,
   camera, Frigate, and administrator values, **Then** Osprey writes a complete configuration,
   generates required deployment secrets, and reports setup success.
2. **Given** invalid or unreachable values, **When** the operator submits the wizard,
   **Then** Osprey identifies the failing field or dependency, does not write a partial
   configuration, and allows correction.
3. **Given** setup has completed, **When** an operator requests the setup route again,
   **Then** the completed wizard cannot overwrite the active configuration without an explicit
   supported reset procedure.

### User Story 2 - Diagnose a deployment (Priority: P1)

As an operator, I can run one diagnostic command and receive actionable results about ports,
routing, the available container runtime, callback reachability, and configuration so that I
can fix a deployment without guessing.

**Why priority**: Most installation failures occur at network or environment boundaries that
are otherwise difficult to distinguish from camera or Frigate failures.

**Independent Test**: Run diagnostics against a healthy deployment and against fixtures with
one deliberate failure in each check; verify stable exit status, clear failure descriptions,
and remediation guidance.

**Acceptance Scenarios**:

1. **Given** a healthy deployment, **When** the operator runs diagnostics, **Then** every
   applicable check is reported as passing and the command exits successfully.
2. **Given** a blocked port, unreachable callback, missing runtime, or invalid configuration,
   **When** the operator runs diagnostics, **Then** the affected check fails with the observed
   condition and a concrete next step, while unrelated checks still run.
3. **Given** an optional dependency is intentionally absent, **When** diagnostics run,
   **Then** the result distinguishes an unavailable optional capability from a broken required
   dependency.

### User Story 3 - Understand and control protocol exposure (Priority: P2)

As an operator connecting Frigate and other clients, I can see which ONVIF and media
authentication modes are active and what network exposure they imply, so that I can choose
compatibility or stronger protection deliberately.

**Why this priority**: Frigate interoperability may require anonymous ONVIF access, while
media and administrative interfaces can expose sensitive camera data.

**Independent Test**: Inspect a newly generated configuration and a configuration with each
optional authentication mode enabled; verify that the active mode, generated credentials,
and required network boundaries are visible without revealing secrets.

**Acceptance Scenarios**:

1. **Given** the compatibility default, **When** the operator views security guidance,
   **Then** anonymous ONVIF behavior and its network-isolation requirement are explicit.
2. **Given** authentication is enabled, **When** a supported client connects with valid and
   invalid credentials, **Then** valid access succeeds, invalid access is rejected, and the
   failure does not disclose the configured secret.
3. **Given** generated secrets exist, **When** logs, status pages, and diagnostic output are
   reviewed, **Then** secret values are absent.

## Edge Cases

- A browser closes or loses connectivity while setup is being saved; the next request must
  report whether setup completed without leaving an ambiguous half-written state.
- A configured Frigate URL resolves but its callback path is unreachable; diagnostics must
  distinguish routing failure from an invalid URL.
- The selected bind address is unavailable or conflicts with another listener; setup and
  diagnostics must report the conflict without stopping unrelated checks.
- A camera exposes fewer PTZ or media capabilities than expected; Osprey must preserve a
  usable read-only or reduced-capability state and explain what is unavailable.
- A deployment is restarted while an authentication challenge or diagnostic check is active;
  stale state must expire safely and not grant access.

## Requirements

### Functional Requirements

- **FR-001**: Osprey MUST provide a browser-based first-run flow that collects bind settings,
  camera identity, external Frigate location, and an administrator password.
- **FR-002**: Osprey MUST validate required values before committing configuration and MUST
  generate all required deployment secrets and credential files with restrictive permissions.
- **FR-003**: Osprey MUST prevent a completed first-run flow from silently overwriting active
  configuration.
- **FR-004**: Osprey MUST provide one diagnostic command that checks configuration validity,
  listener availability, routing, callback reachability, and available container tooling.
- **FR-005**: Diagnostic output MUST use stable pass, warning, and failure results, return a
  non-success status when a required check fails, and include actionable remediation text.
- **FR-006**: Osprey MUST clearly report active ONVIF and media authentication modes without
  printing credential values.
- **FR-007**: Osprey MUST preserve the documented anonymous-ONVIF compatibility mode while
  providing an explicitly configurable authenticated mode where supported by clients.
- **FR-008**: Authentication challenges, callbacks, setup writes, and diagnostic checks MUST
  expire or fail safely when inputs are malformed, stale, or interrupted.
- **FR-009**: Generated configuration and deployment artifacts MUST use neutral examples and
  MUST NOT contain personal infrastructure identifiers or real credentials.
- **FR-010**: The onboarding and diagnostic behavior MUST be documented for supported public
  installation methods and tested in automated unit and integration coverage.

### Key Entities

- **Deployment configuration**: The operator-selected bind, camera, Frigate, protocol, and
  security settings required to run Osprey.
- **Deployment secret**: A generated or supplied credential used to protect an administrative,
  ONVIF, or media boundary.
- **Diagnostic check**: A named validation with a status, observed evidence, and remediation
  guidance.
- **Protocol exposure**: The network listener and authentication mode through which a client
  reaches Osprey or its media stream.

## Success Criteria

- **SC-001**: A first-time operator can complete valid setup from an empty data directory in
  under five minutes without manually editing a generated configuration file.
- **SC-002**: Each required diagnostic check completes within 10 seconds on a reachable local
  deployment, and a single failed check does not suppress results for other checks.
- **SC-003**: 100% of automated setup and diagnostic tests cover valid input, malformed input,
  unreachable dependencies, interrupted writes, and secret-redaction behavior.
- **SC-004**: An operator can identify the failing boundary and its next remediation step from
  diagnostic output alone in at least 90% of controlled failure scenarios.
- **SC-005**: Repository documentation describes every supported installation path, required
  network exposure, authentication choice, and external Frigate dependency.

## Assumptions

- Operators have an existing Frigate installation; Osprey does not install or manage Frigate.
- The operator can reach the Osprey administrative interface during first-run setup.
- Camera discovery and control capabilities vary by model and firmware; reduced capability is
  preferable to an unsafe or misleading failure.
- Deployment secrets are stored in the operator's protected persistent data location and are
  backed up or rotated according to the deployment's policy.
- Public release artifacts are reviewed separately for personal infrastructure and secret
  leakage before publication.
