# Changelog

All notable changes to Osprey are listed here. Entries are generated from
Conventional Commit subjects by git-cliff (see cliff.toml).

## Unreleased

### Features
- **onvif:** A media persona for NVRs. `ports.onvif_read_only` now serves the
  camera as a second ONVIF device — the same media, no PTZ at all — with its
  own serial, MAC, hostname and friendly name (`<camera name> (media)`), so an
  NVR such as UniFi Protect records it as a camera of its own and can never
  move the camera head. WS-Discovery answers with one ProbeMatch per device,
  the media persona first, and a probe that asks for a PTZ device is answered
  by the primary persona alone. See docs/protect-dual-adoption.md.

### Fixes
- **discovery:** A discovery message that names every persona now stays inside
  one datagram — the 1472 bytes a 1500-byte MTU leaves for the UDP payload —
  so it is not split into IP fragments, which some networks drop and some
  clients reassemble badly. The metadata version, the device type and then the
  `To` header are given up, in that order, and only when the personas would
  not otherwise fit; a controller that serves one camera still sends the full,
  standard message.

## [0.9.0b1] - 2026-11-20

First beta. G5 PTZ fully supported; other UniFi PTZ models experimental.

### Features
- **sentry:** Idle PTZ patrol mode. Set named preset points per camera and
  the camera cycles between them at a fixed cadence and speed while idle. The
  patrol yields to a manual hold and to Frigate autotracking: autotracking
  commands take priority, and the patrol waits out `tracking_hold_sec` before
  resuming its sweep. Configurable per camera, with its own points, cadence,
  speed and tracking hold. See docs/sentry.md.
- **sentry:** Patrols survive a restart. The operator config file is the
  baseline and runtime changes (including patrols removed at runtime) are
  persisted to `<state_dir>/sentry.json`.
- **sentry:** Patrol control and status are exposed on all four surfaces:
  the operator config file, the REST API, the operator web console (Patrol
  panel) and MQTT (`command/sentry`, `state/sentry`).
- **sentry:** A third arbitration source, `patrol`, joins `manual` and
  `auto`, so the controller can tell a patrol move apart from an autotracking
  move and record autotracking activity per camera.
- **ptz:** `goto_preset` reports a refused move instead of silently
  succeeding, so a patrol never consumes a tour point for a move that never
  went out.
- **ptz:** Manual wins, then resume. A person's PTZ command (console, MQTT, a
  non-autotrack ONVIF user such as Protect) overrides Frigate and holds it off for
  `ptz.manual_hold_sec` (default 30 s). New `ptz.autotrack_users` setting.
- **protect-guard:** `osprey protect-guard --check/--apply` checks for or
  creates the UniFi firewall policy that keeps Protect from adopting the
  physical camera; `osprey doctor` and startup warn when the read-only ONVIF
  persona is on. See docs/protect-guard.md.
- **probe:** `osprey probe` writes a redacted, read-only data bundle for an
  unsupported UniFi PTZ model. Models other than the G5 PTZ are marked
  experimental (log warning, `support` in the operator API).
- **onvif:** `onvif.log_calls` logs every SOAP call with user, peer and PTZ source.
- **health:** Degrade `/healthz` on stalled video and add `/metrics`.

### Packaging
- **logs:** Application logs persist to a rotating file in the state volume
  (`<state_dir>/logs/osprey.log`, 5 MiB x 3) instead of being lost with the
  container. Log output is redacted before it is written.
- Version 0.9.0b1. Release workflow accepts PEP 440 pre-release tags and
  publishes them as pre-releases (image tags `0.9.0b1` and `v0.9.0b1`; `latest`
  moves only on final releases).
- **npm:** Restored the `@osprey-ptz/cli` channel. The launcher runs the
  entrypoint rather than a non-existent binary and exposes `doctor`, `probe`,
  `protect-guard` and `version`.
- **chocolatey:** Restored the Chocolatey channel with the `osprey` package.
- **homebrew:** The Homebrew tap ships a formula for the CLI.
- **homeassistant:** The add-on is published as a Home Assistant app, and the
  documentation uses the current 2026.11 apps terminology.
- **homeassistant:** The app repository `grayslawson/osprey-ha-apps` exists
  and is kept in step by a scheduled mirror, so the app can be installed from
  Home Assistant. The add-on pins the image it installs to the add-on version
  instead of pulling whatever was published last.
- Issue forms: bug report and camera model support.

### Repository
- GitHub `grayslawson/osprey` is the canonical repository. The Forgejo
  instance is a read-only pull mirror of it, synchronised hourly; the
  pre-rename Forgejo tree is kept frozen as `grays/osprey-legacy`.
  See docs/repository-sync.md.

### Renamed: cuckoo to osprey
- The controller source moved from `cuckoo/` to `osprey/`. Deployments that
  mount or run `cuckoo/main.py` directly must switch to `osprey/main.py`. The
  release image, Home Assistant add-on, NixOS package and PyPI wheel are updated.
- Default file names are now `osprey.json` and `osprey.pem`. An existing
  `cuckoo.json` or `cuckoo.pem` is still used when the new file does not exist,
  so an upgraded camera keeps trusting its certificate.
- Logger names changed from `cuckoo.*` to `osprey.*`. Update any log filters.
- `handoff.sh` reads `OSPREY_*` variables and falls back to the old `CUCKOO_*` names.
- The WS-Discovery device UUID is unchanged, so NVRs keep the same device.

### Bug fixes
- **sentry:** An autotracker now takes the camera from a patrol that is still
  travelling to its next point. The first autotracking command of a detection
  used to be refused as "the camera is already moving" for as long as the head
  was moving, so the tracking never started.
- **homeassistant:** The release workflow's add-on version check ran a shell
  expression whose quoting built a different regular expression than it
  appeared to, and the Home Assistant image job failed. The check now runs,
  and a test executes the workflow step for real.
- **homeassistant:** A pre-release no longer moves the `latest` add-on image
  tag, so a beta cannot become the add-on that an existing install pulls.
- **ptz:** Compare the recovery re-arm deadline without float subtraction.
- **ptz:** Chain FOV-scaled `ContinuousMove` steps and avoid reversing on Stop.
- **ptz:** Cap camera reboot requests and back off the recovery re-arm.
- **ptz:** Require a minimum silence before the unanswered-count watchdog recycle.
- **ptz:** Do not gate the capped reboot on stalled video.
- **ptz:** Recover a wedged `ptz1` channel and persist presets.
- **onvif:** Fail closed on partial authentication config.
- **onvif:** Allow-list home position ops; answer stalled oversized requests.
- **onvif:** Bound authentication replay state.
- **webapi:** Narrow the watchdog recovery dict so mypy passes.
- **ui:** Inline preset management on the Live tab and surface PTZ watchdog health.
- **adoption:** Disarm every track instead of only the last.
- **rtsp:** Harden RTSP sessions for stalls, slow clients and RTCP.
- **rtsp:** Bound the digest nonce state and tighten digest validation.
- **rtsp:** Answer 503 + `Retry-After` for armed tracks not yet created.
- **ui:** Close the completed setup endpoint and clean up WebSocket teardown.
- **proxmox:** Generate a valid host-network compose with camera ports.

### Documentation
- Add docs/sentry.md and index it from the documentation root.
- Document MQTT sentry commands and state.
- Normalize the Protect dual-adoption note.
- Clarify the published image onboarding.
- Document Home Assistant Frigate deployment.
- Clarify separate RTSP authentication.
- Add package installation commands.
- Clarify package publication boundaries.
- **license:** Unify copyright notices and add NOTICE.
