# Osprey documentation

## Set up

- [Installation](installation.md): container, Home Assistant, NixOS, Proxmox,
  the wheel and package managers, updates and rollback.
- [Upgrading](upgrading.md): backing up state, moving to a new release and
  rolling back.
- [Uninstall and return to Protect](uninstall.md): hand the camera back to
  UniFi Protect and remove Osprey.
- [NixOS](nixos.md): the flake package and module.
- [Configuration](configuration.md): config keys, environment variables and
  flags.
- [Onboarding checks](onboarding-runbook.md): local preflight before a camera
  is connected.
- [Camera models](camera-models.md): what is supported, and `osprey probe`.
- [Multiple cameras](multi-camera.md): the camera allow-list and per-camera
  routes.

## Use with Frigate and Protect

- [Frigate](frigate-integration.md): the camera entry, codecs, presets,
  autotracking, calibration and manual hold.
- [Frigate connection settings](frigate-connection.md): the Frigate URL API.
- [Sentry patrol](sentry.md): idle preset tours and the Frigate zone caveat.
- [UniFi Protect alongside Frigate](protect-dual-adoption.md): the media-only
  ONVIF device.
- [Protect guard](protect-guard.md): keep Protect from adopting the camera.
- [MQTT](mqtt.md): commands and state topics.
- [Assist](assist.md): the optional image-quality helper and the doors it uses.
- [Survey](survey.md): one button that maps what the camera sees and proposes presets and a patrol.

## Operate

- [Troubleshooting](troubleshooting.md): symptoms, causes and fixes.
- [FAQ](faq.md): short answers to the usual questions.
- [Verify a deployment](installation.md#verify-a-deployment): `osprey doctor`,
  the first stop when something does not work.
- [API reference](api.md): the operator API, health endpoints and ONVIF paths.
- [Operator console](operator-ui.md): the browser app and its API.
- [PTZ health](ptz-health.md): motion rules, watchdog, `/healthz`, `/metrics`
  and the `home` preset.
- [Authentication](authentication.md): console password, sessions, ingress and
  ONVIF users.
- [Security](security.md): trust boundaries and what each port exposes.

## Develop

- [Architecture](../osprey/ARCHITECTURE.md): the controller and ONVIF layers.
- [Controller README](../osprey/README.md): running from source and protocol
  notes.
- [Tests](../osprey/tests/README.md) and
  [harnesses](../osprey/harness/README.md).
- [Console design notes](../DESIGN.md).
- [Quality map](quality.md): evidence per domain.
- [Contributing](../CONTRIBUTING.md) and [agent guide](../AGENTS.md).

When behavior changes, update the guide that covers it and add new guides to
this index. Keep credentials, private addresses and recordings out of every
page.
