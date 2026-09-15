<!--
Sync Impact Report
- Version change: template → 1.0.0
- Modified principles: none; this is the initial Osprey constitution.
- Added sections: Security and Compatibility Constraints; Development Workflow.
- Removed sections: none.
- Follow-up TODOs: none.
-->

# Osprey Constitution

## Core Principles

### I. Safe Camera Control and Compatibility

Osprey MUST treat camera movement as safety-sensitive control. PTZ commands MUST be
bounded, validated, cancellable, and isolated per camera. Tracking and preset logic MUST
preserve the camera's recoverable home position and MUST fail safely when position,
capability, or reachability is unknown. Osprey MUST remain an integration layer for an
existing Frigate installation; it MUST NOT require, bundle, or silently reconfigure
Frigate. ONVIF and media behavior MUST follow documented interoperability contracts,
including explicit handling of anonymous and authenticated modes.

### II. Secure by Explicit Trust Boundaries

Every network listener, credential, callback, and control path MUST have a documented
trust boundary. Secrets MUST be generated or supplied through protected configuration,
MUST NOT be logged, committed, or included in images, and MUST be replaceable without
changing application code. Optional unauthenticated compatibility modes MUST be clearly
opt-in or explicitly documented with their risks, and deployments MUST provide a safer
authenticated alternative where protocol clients support it. Inputs from cameras,
Frigate, MQTT, browsers, and discovery MUST be validated before use.

### III. Testable and Observable Behavior

New or changed behavior MUST have automated tests at the narrowest useful layer and
integration or contract coverage for ONVIF, RTSP, Frigate, MQTT, and camera boundaries
when those boundaries are affected. Tests MUST cover success, timeout, malformed input,
loss of connectivity, cancellation, and recovery paths appropriate to the change.
Runtime failures MUST be diagnosable through structured logs, health information, and
operator-visible warnings without exposing secrets or unnecessary personal data.

### IV. Operable Across Supported Installations

An operator MUST be able to discover prerequisites, configure Osprey, verify connectivity,
and recover from common failures using the documented first-run flow, diagnostics, and
deployment instructions. Supported installation artifacts MUST be reproducible and
version-aligned. Documentation MUST state what Osprey owns, what remains external, and
which network ports, credentials, and permissions are required. Changes to one supported
installation method MUST NOT silently break the others.

### V. Small, Reviewable, User-Value-Driven Changes

Work MUST be scoped to a clear user outcome and preserve existing public contracts unless
a breaking change is explicitly proposed. Design and implementation MUST favor the
smallest maintainable solution that meets the requirement. Feature work MUST be
traceable through a specification, technical plan, task list, implementation, and
verification; unrelated cleanup MUST be separated. Generated artifacts and dependencies
MUST be justified and kept current.

## Security and Compatibility Constraints

- Osprey MUST never assume that a camera, Frigate instance, MQTT broker, or UniFi
  console is reachable, trusted, or fully capable; capability discovery and failure
  handling are required.
- PTZ movement, tracking, and zoom MUST enforce configured limits, rate limits, and
  stale-command protection so a lost target or repeated event cannot cause runaway
  motion.
- Media re-publishing MUST be read-only with respect to camera control and MUST have
  independently documented authentication, exposure, and transport behavior.
- Configuration examples MUST use neutral addresses and placeholder credentials. Real
  homelab identities, tokens, recordings, or personal infrastructure MUST remain out of
  public artifacts.

## Development Workflow

- Before implementation, record the user outcome, acceptance scenarios, constraints,
  and error cases in the applicable Spec Kit artifacts.
- Plans MUST identify affected interfaces, security boundaries, compatibility risks,
  migration or rollback behavior, and the tests that prove the change.
- Implementations MUST keep task status current and MUST run focused tests before the
  full relevant validation suite.
- A release MUST include reproducible build or packaging checks, documentation updates,
  and a review of secret handling, network exposure, and backward compatibility.
- A change is not complete until its acceptance criteria are verified or an explicit
  residual risk is documented for review.

## Governance

This constitution is the highest-level project guidance. If another document conflicts
with it, the conflict MUST be resolved in favor of this constitution or recorded as an
approved amendment before implementation proceeds.

Amendments require a written rationale, an updated Sync Impact Report, review of affected
specifications and workflows, and a semantic version increment. MAJOR increments remove
or redefine a non-negotiable principle; MINOR increments add a principle or materially
expand governance; PATCH increments clarify wording without changing obligations.

Every pull request or release review MUST check applicable principles, tests, security
boundaries, documentation, and supported installation artifacts. Exceptions MUST name
the violated principle, explain the risk, define a mitigation or sunset date, and receive
explicit maintainer approval. The constitution MUST be revisited when a protocol,
installation target, authentication model, or camera-control contract changes.

**Version**: 1.0.0 | **Ratified**: 2026-09-15 | **Last Amended**: 2026-09-15
