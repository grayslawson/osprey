# Changelog

All notable changes to Osprey are listed here. Entries are generated from
Conventional Commit subjects by git-cliff (see cliff.toml).

## [0.9.0b3] - 2026-11-21

### Bug fixes
- **homeassistant:** The app can be installed. Its manifest named a tagged
  image, which the Supervisor rejects ("Docker image must not contain a
  tag"); it now names the untagged image and the Supervisor pulls it at the
  app's version.
- **homeassistant:** `init: false`, since the image runs its own tini, and
  the unused `config:rw` mapping is gone.
- **doctor:** Inside the release image (and so through npm and Chocolatey)
  the camera route, callback and Frigate checks fall back to Python instead
  of failing for lack of `ip` and `curl`. Run from a launcher-only install, a
  config file it cannot validate is a warning, not an error.

### Documentation
- Every install path was walked end to end as a new user. Gaps fixed: the
  compose `.env`, the ONVIF user Frigate needs for autotracking, wheel and
  npm installs from the release assets, the Chocolatey local source, and
  signing in to the sentry API with `curl`.

## [0.9.0b2] - 2026-11-21

### Features
- **onvif:** Media persona for NVRs. `ports.onvif_read_only` serves the camera
  as a second ONVIF device with the same media and no PTZ. It has its own
  serial, MAC, hostname and friendly name (`<camera name> (media)`), so an NVR
  such as UniFi Protect records it as a separate camera and cannot move the
  head. WS-Discovery answers with one ProbeMatch per device, media persona
  first, and a probe for a PTZ device gets the primary alone. See
  docs/protect-dual-adoption.md.

### Bug fixes
- **setup:** The wizard works in a browser. Its inline script was blocked by
  the console's content security policy, so the form fell back to a GET that
  put the admin password and setup token in the URL. The form now always
  POSTs.
- **mqtt:** Command topics are subscribed once the broker connects, and again
  after every reconnect. Before, commands sent over MQTT were never received.
- **onvif:** With ONVIF authentication on, snapshot URIs handed out by the
  media persona were refused with 401.
- **onvif:** Non-ASCII tokens, digest responses and CSRF headers are refused
  instead of dropping the connection with a traceback.
- **config:** A malformed `osprey.json`, or a bad `mqtt` or `discovery`
  value, exits with a clear error. Command-line ports no longer leak into the
  `osprey.json` that the setup wizard writes.
- **console:** Clearing the Frigate address works. The Patrol page says when
  no camera is connected instead of loading forever.
- **homeassistant:** The app starts under the Supervisor. It could not create
  its state directory on the root-owned `/data`, and it now runs as an
  unprivileged user after preparing it. The sidebar console is signed in
  through Home Assistant ingress, and a new `admin_password` option protects
  the direct port. The app no longer writes every camera message to an
  unbounded, unredacted file.
- **packaging:** `osprey doctor` works from the PyPI wheel, and a missing
  Compose binary is a warning, not an error, so doctor passes on native
  installs and inside the container. Chocolatey ships one install script,
  adds an `osprey` command for the compose file, and removes it on uninstall.
- **release:** The npm job can run (its `setup-node` pin named a
  `setup-python` commit) and publishes pre-releases on the `next` dist-tag.
  Pre-release images get the `v`-prefixed tag as well. Pushes to `main` no
  longer move `latest`. The Homebrew asset carries the checksum of the tagged
  archive, and `SHA256SUMS` covers every asset.
- **discovery:** A reply that names both personas now fits in one datagram
  (1472 bytes on a 1500-byte MTU), so it is not split into IP fragments that
  some networks drop. When needed, the reply drops, in order, the metadata
  version, the device type, the `To` header, the XML declaration and the
  hardware scope. A controller with one camera still sends the full message.

## [0.9.0b1] - 2026-11-20

First beta. G5 PTZ fully supported; other UniFi PTZ models experimental.

### Features
- **sentry:** Idle PTZ patrol. Each camera cycles through named presets at
  its own cadence and speed while idle. Frigate autotracking and a manual hold
  take priority, and the patrol waits `tracking_hold_sec` after tracking ends.
  See docs/sentry.md.
- **sentry:** Patrols survive a restart. `osprey.json` is the baseline, and
  runtime changes, including removals, are saved to `sentry.json` in the
  state directory.
- **sentry:** Patrols can be controlled from the config file, the REST API,
  the console's Patrol page and MQTT (`command/sentry`, `state/sentry`).
- **sentry:** A third arbitration source, `patrol`, joins `manual` and `auto`,
  so a patrol move is told apart from an autotracking move and autotracking
  activity is recorded per camera.
- **ptz:** `goto_preset` reports a refused move instead of returning success,
  so a patrol never spends a point on a move that did not happen.
- **ptz:** Manual wins, then resume. A person's PTZ command (console, MQTT, or
  a non-autotrack ONVIF user such as Protect) overrides Frigate and holds it
  off for `ptz.manual_hold_sec` (default 30 s). New `ptz.autotrack_users`
  setting.
- **protect-guard:** `osprey protect-guard` checks for, and with `--apply`
  creates, the UniFi firewall policy that keeps Protect from adopting the
  physical camera. `osprey doctor` and startup warn while the read-only ONVIF
  persona is on. See docs/protect-guard.md.
- **probe:** `osprey probe` writes a redacted, read-only data bundle for an
  unsupported UniFi PTZ model. Models other than the G5 PTZ are marked
  experimental (a log warning and `support` in the operator API).
- **onvif:** `onvif.log_calls` logs every SOAP call with user, peer and PTZ
  source.
- **health:** `/healthz` degrades on stalled video, and `/metrics` is new.

### Packaging
- **logs:** Logs persist to a rotating, redacted file in the state volume
  (`<state_dir>/logs/osprey.log`, 5 MiB x 3).
- Version 0.9.0b1. The release workflow accepts PEP 440 pre-release tags and
  publishes them as pre-releases, with image tags `0.9.0b1` and `v0.9.0b1`.
  `latest` moves only on final releases.
- **npm:** `@osprey-ptz/cli` is back. It runs the image entrypoint and offers
  `doctor`, `probe`, `protect-guard` and `version`.
- **chocolatey:** The `osprey` Chocolatey package is back.
- **homebrew:** The Homebrew tap ships a formula for the CLI.
- **homeassistant:** The add-on is published as a Home Assistant app, using
  the 2026.11 "apps" wording.
- **homeassistant:** The app repository `grayslawson/osprey-ha-apps` is live
  and kept in step by a scheduled mirror. The add-on pins its image to the
  add-on version.
- Issue forms for bug reports and camera model support.

### Repository
- GitHub `grayslawson/osprey` is the canonical repository. Forgejo holds a
  read-only pull mirror, synchronised hourly, and keeps the pre-rename tree
  frozen as `grays/osprey-legacy`. See docs/repository-sync.md.

### Renamed: cuckoo to osprey
- The controller source moved from `cuckoo/` to `osprey/`. Deployments that
  run `cuckoo/main.py` directly must switch to `osprey/main.py`. The image,
  Home Assistant add-on, NixOS package and PyPI wheel are updated.
- Default file names are now `osprey.json` and `osprey.pem`. An existing
  `cuckoo.json` or `cuckoo.pem` is used when the new file is missing, so an
  upgraded camera keeps trusting its certificate.
- Logger names changed from `cuckoo.*` to `osprey.*`. Update log filters.
- `handoff.sh` reads `OSPREY_*` variables and falls back to `CUCKOO_*`.
- The WS-Discovery device UUID is unchanged, so NVRs keep the same device.

### Bug fixes
- **sentry:** An autotracker now takes the camera from a patrol that is still
  travelling. The first tracking command of a detection was refused as "the
  camera is already moving" while the head moved, so tracking never started.
- **homeassistant:** The release workflow's add-on version check built a
  different regular expression than it appeared to, and the image job failed.
  The check now works, and a test runs the workflow step.
- **homeassistant:** A pre-release no longer moves the `latest` add-on image
  tag, so existing installs never pull a beta.
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
