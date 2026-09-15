# Research: Release Onboarding and Diagnostics

## Decision: Reuse the existing first-run setup manager

**Rationale**: `cuckoo/setup_wizard.py` already validates host, bind address, camera identity,
Frigate URL, and administrator password. It uses temporary files, restrictive permissions,
`fsync`, and atomic replacement, which satisfies the partial-write and secret-storage needs.
The manager also refuses a second setup save after completion.

**Alternatives considered**: A separate provisioning service would duplicate validation and
create another secret boundary. Manual configuration would fail the public-release onboarding
goal.

## Decision: Keep diagnostics as a read-only POSIX command

**Rationale**: `scripts/osprey-doctor.sh` can run in a host, container, or package installation
without importing the application server. It already checks advertised host validity, runtime
configuration, Docker/Podman Compose, local TCP ports, camera routing, and Frigate reachability.
Its pass/warning/error output and exit status are suitable for automation.

**Alternatives considered**: An HTTP-only health endpoint cannot inspect host tools or listener
collisions reliably. A Python-only doctor would make minimal installations less portable.

## Decision: Preserve independent ONVIF and RTSP authentication

**Rationale**: Frigate commonly requires anonymous ONVIF during compatibility setup, while RTSP
and administrative access have separate protection needs. Existing configuration and security
documentation already describe optional ONVIF UsernameToken and RTSP Digest modes. The plan
therefore verifies redaction and documentation rather than coupling the protocols.

**Alternatives considered**: Enforcing one shared credential would break clients and obscure the
actual network trust boundary. Making authentication mandatory by default would violate the
known Frigate interoperability constraint.

## Decision: Treat Frigate as an external dependency

**Rationale**: The public product controls and republishes a camera for an existing Frigate
installation. The setup wizard records the Frigate URL and the doctor checks reachability, but
Osprey does not install, configure, or mutate Frigate.

**Alternatives considered**: Bundling Frigate would enlarge the product scope, complicate
upgrades, and contradict the documented integration model.

## Decision: Validate through existing test and packaging gates

**Rationale**: The repository already has focused Python tests, static typing, shell checks,
container builds, Home Assistant validation, and package metadata checks. The feature's
acceptance is demonstrated by those gates plus the quickstart scenarios.

**Alternatives considered**: A new test framework or end-to-end hardware requirement would make
public contributions and reproducible CI unnecessarily difficult.
