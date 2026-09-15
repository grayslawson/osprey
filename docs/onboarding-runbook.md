# Osprey onboarding and diagnostics

Run these checks before connecting a camera. They are local and read-only: no
camera is adopted, no Protect or Frigate service is changed, and no deployment
secret is read.

## Start with the checkout you intend to run

From the repository root, verify the revision before building. A stale checkout
is a common source of confusing option and Compose errors:

```sh
git rev-parse --show-toplevel
git status --short
git log -1 --oneline
./docker/onboarding_preflight.sh [cuckoo.json]
```

The preflight rejects malformed JSON and credentials accidentally placed in a
runtime config. It labels the result **LOCAL/TEST**; it is not a production
readiness or camera-network check.

## Configuration and secrets

`cuckoo.json` contains non-secret runtime settings only: host, ports, camera
MAC allow-list, and codec tracks. Generate JSON with a JSON encoder rather than
shell interpolation so names containing quotes, backslashes, or newlines remain
valid. Keep camera, Protect, MQTT, and admin credentials in your deployment's
secret manager or in a root-owned file outside this repository.

If your deployment reports a secret-manager or key error, stop and check the
host's key path, identity, and file permissions. Do not copy keys into this
checkout or work around the failure by committing plaintext secrets.

## Deployment boundary

Service ordering, maintenance-window restarts, secret-manager wiring, firewall
rules, and external Frigate configuration belong in your deployment system, not
in this repository. Treat a release as ready only after its image or checkout
has been deliberately promoted and validated. Never use `handoff.sh` against a
production camera as part of routine validation. ONVIF authentication is
opt-in; leave it disabled only when the Frigate client is known to require
anonymous ONVIF, and test interoperability before changing it.

## First-run recovery

The wizard validates and stages the runtime JSON, Frigate metadata, and generated secret
files together. If the process exits during a save, restart Osprey and check setup status
before submitting again. A failed save leaves setup pending; do not copy temporary files.
If the one-time setup token is lost, restart Osprey and read the new token from the service
log. After setup completes, use the documented configuration-edit and restart procedure
instead of reopening the wizard.
