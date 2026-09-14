# Osprey onboarding and diagnostics

This repository is the public Osprey controller. The commands below are safe
local checks; they do not adopt a camera, change Protect, contact Frigate, or
read SOPS/deployment secrets.

## Start with the checkout you intend to run

Run from the repository root and verify the revision before building. A stale
checkout is a common source of confusing option and Compose errors:

```sh
git rev-parse --show-toplevel
git status --short
git log -1 --oneline
./docker/onboarding_preflight.sh [cuckoo.json]
```

The preflight rejects malformed JSON and credentials accidentally placed in a
runtime config. It labels its result **LOCAL/TEST**. It is not a production
readiness or camera-network check.

## Configuration and secrets

`cuckoo.json` contains non-secret runtime settings only (host, ports, camera
MAC allow-list, and codec tracks). Generate JSON with a JSON encoder rather
than shell interpolation so names containing quotes, backslashes, or newlines
remain valid. Keep Protect credentials in the deployment's root-owned secret
store; this repository never needs a SOPS key to run its local test stack.

If a deployment reports “SOPS key not found”, stop and have the pd-nixos owner
check the host's key path, age identity, and file permissions. Do not copy keys
into this checkout or work around the failure by committing plaintext secrets.

## Production boundary

Production service ordering, camera-silo gates, maintenance-window restarts,
SOPS wiring, firewall rules, and external Frigate configuration belong in
**pd-nixos**, not here. An Osprey change is complete only after its image or
checkout is deliberately promoted by that deployment. Never use `handoff.sh`
against a production camera as part of a routine validation run.

For an ONVIF “unauthenticated” or empty-credentials prompt, use the values
required by the client and deployment policy; this Osprey ONVIF surface does
not currently implement ONVIF authentication. An empty password is not a
signal to guess, reset, or re-adopt the camera.
