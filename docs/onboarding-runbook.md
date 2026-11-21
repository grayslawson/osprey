# Onboarding checks

Run these before connecting a camera. They are local and read-only: they
adopt no camera, change no Protect or Frigate setting and read no secret.

## Preflight a checkout

From the repository root, confirm you are on the revision you mean to run,
then check the config file:

```sh
git status --short
git log -1 --oneline
./docker/onboarding_preflight.sh osprey.json
```

The preflight rejects malformed JSON and top-level credential keys in the
config. Its result is labelled **LOCAL/TEST**; it does not check the camera
network. For a deployment, run [`osprey doctor`](installation.md#verify-a-deployment).

## Keep secrets out of the config

`osprey.json` holds non-secret settings: host, ports, the camera allow-list
and tracks. Generate it with a JSON encoder rather than shell string
interpolation, so names with quotes or backslashes stay valid. Keep camera,
Protect and admin credentials in your secret manager or a root-only file
outside the repository. The MQTT password is the one exception; see
[MQTT](mqtt.md).

If your deployment reports a secret-manager or key error, check the key path,
identity and file permissions. Do not copy keys into the checkout.

## Deployment boundary

Service ordering, restarts, secret-manager wiring, firewall rules and Frigate
configuration belong to your deployment, not to this repository. Never run
`osprey/handoff.sh` against a production camera as part of routine testing.

## Setup wizard recovery

The wizard writes the configuration, the Frigate URL and the secret files
together. If Osprey exits during a save, restart it and check setup status
before you submit again. A failed save leaves setup pending; do not copy
temporary files. A lost setup token is replaced on restart. Once setup is
complete, edit the configuration and restart instead of reopening the wizard.
