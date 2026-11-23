# Changelog

Every release of Osprey, newest first. Each section is generated from the
Conventional Commit titles merged for that release, and it is also the body
of the GitHub release. See cliff.toml and CONTRIBUTING.md. The sections for
0.9.0b1 to 0.9.0b3 were written by hand, before the notes came from git-cliff.

## [0.9.0b3] - 2026-11-21

### Fixes
- The Home Assistant app can be installed. Its manifest named a tagged image,
  and the Supervisor rejects that with "Docker image must not contain a tag".
  It now names the image without a tag, and the Supervisor pulls it at the
  app's version.
- The app starts one init process instead of two, and no longer maps the Home
  Assistant configuration folder, which it never used.
- `osprey doctor` runs inside the release image, so it also works through npm
  and Chocolatey. The camera route, callback and Frigate checks fall back to
  Python when `ip` and `curl` are missing. On an install that only has the
  launcher, a config file it cannot validate is a warning, not an error.

### Docs
- Every install path was walked from start to finish as a new user, and the
  gaps are fixed: the Compose `.env` file, the ONVIF user that Frigate needs
  for autotracking, installing the wheel and the npm package from the release
  assets, the Chocolatey local source, and signing in to the sentry API with
  `curl`.

Full list of changes: [v0.9.0b2...v0.9.0b3](https://github.com/grayslawson/osprey/compare/v0.9.0b2...v0.9.0b3)

## [0.9.0b2] - 2026-11-21

### Features
- Media persona for NVRs. Set `ports.onvif_read_only` and Osprey serves the
  camera a second time as its own ONVIF device, with the same video and no
  PTZ. It has its own serial, MAC, hostname and friendly name (`<camera name>
  (media)`), so an NVR such as UniFi Protect records it as a separate camera
  and cannot move the head. WS-Discovery answers with one ProbeMatch per
  device, media persona first, and a probe for a PTZ device gets the primary
  device alone. See `docs/protect-dual-adoption.md`.
- The Home Assistant app repository is published, and the app pins its image to
  the app version. The app's options have readable names in the Configuration
  tab.
- The release workflow pushes the Homebrew formula and the Chocolatey package
  to their repositories.

### Fixes
- The setup wizard works in a browser. Its inline script was blocked by the
  console's content security policy, so the form fell back to a GET request
  that put the admin password and setup token in the URL. The form now always
  POSTs.
- MQTT commands are received. Command topics are subscribed once the broker
  connects, and again after every reconnect. Before, commands sent over MQTT
  never arrived.
- With ONVIF authentication on, the snapshot URIs that the media persona hands
  out were refused with 401.
- Non-ASCII tokens, digest responses and CSRF headers are refused, instead of
  dropping the connection with a traceback.
- A malformed `osprey.json`, or a bad `mqtt` or `discovery` value, exits with a
  clear error. Command-line ports no longer leak into the `osprey.json` that
  the setup wizard writes.
- In the console, clearing the Frigate address works, and the Patrol page says
  when no camera is connected instead of loading forever.
- The Home Assistant app starts under the Supervisor. It could not create its
  state directory on the root-owned `/data`, so it now prepares the directory
  as root and runs the controller as an unprivileged user. The sidebar console
  is signed in through Home Assistant ingress, and a new `admin_password`
  option protects the direct port. The app no longer writes every camera
  message to an unbounded, unredacted file.
- `osprey doctor` works from the wheel. A missing Compose binary is a warning,
  not an error, so doctor passes on native installs and inside the container.
  The Chocolatey package has one install script, adds an `osprey` command for
  the Compose file, and removes it on uninstall.
- The release workflow can publish npm (its `setup-node` pin named a
  `setup-python` commit), and npm pre-releases go on the `next` dist-tag.
  Pre-release images also get the `v` prefixed tag. Pushes to `main` no longer
  move `latest`. The Homebrew asset carries the checksum of the tagged archive,
  and `SHA256SUMS` covers every asset.
- A WS-Discovery reply that names both devices now fits in one datagram (1472
  bytes on a 1500 byte MTU), so networks that drop IP fragments no longer lose
  it. When it needs room, the reply drops, in order, the metadata version, the
  device type, the `To` header, the XML declaration and the hardware scope. A
  controller with one camera still sends the full message.

### Docs
- Public docs rewritten for the 0.9 beta, with the image tag that every
  release publishes.

Full list of changes: [v0.9.0b1...v0.9.0b2](https://github.com/grayslawson/osprey/compare/v0.9.0b1...v0.9.0b2)

## [0.9.0b1] - 2026-11-20

First beta. The UniFi G5 PTZ is fully supported, and other UniFi PTZ models
are experimental. Pre-release images are tagged `0.9.0b1` and `v0.9.0b1`, and
`latest` does not move for them.

### Breaking changes
- The controller moved from `cuckoo/` to `osprey/`. If you run
  `cuckoo/main.py` yourself, switch to `osprey/main.py`. The image, the Home
  Assistant app, the NixOS package and the wheel already use the new path.
- The default file names are now `osprey.json` and `osprey.pem`. Osprey still
  reads an existing `cuckoo.json` or `cuckoo.pem` when the new file is missing,
  so an upgraded camera keeps trusting its certificate.
- Logger names changed from `cuckoo.*` to `osprey.*`. Update any log filters.
- `handoff.sh` reads `OSPREY_*` variables and falls back to the old `CUCKOO_*`
  names.
- The WS-Discovery device UUID is unchanged, so NVRs keep seeing the same
  device.

### Features
- Sentry patrol. When nothing else is moving the camera, it tours a list of
  named presets at its own pace and speed. Frigate autotracking and a person at
  the console take priority, and the patrol waits `tracking_hold_sec` after
  tracking ends. Set it up in `osprey.json`, through the REST API, on the
  console's Patrol page or over MQTT. Patrols survive a restart. See
  `docs/sentry.md`.
- Manual control wins, then Frigate resumes. A PTZ command from the console,
  MQTT or an ONVIF user that is not an autotracker (such as Protect)
  overrides Frigate and holds it off for `ptz.manual_hold_sec`, 30 seconds by
  default. `ptz.autotrack_users` names the users that count as autotrackers.
- `osprey protect-guard` checks for the UniFi firewall policy that keeps
  Protect from adopting the physical camera, and creates it with `--apply`.
  `osprey doctor` and the startup log warn while the read-only ONVIF device is
  on. See `docs/protect-guard.md`.
- `osprey probe` writes a redacted, read-only data bundle for a UniFi PTZ
  model that Osprey does not know. Models other than the G5 PTZ are marked
  experimental, with a log warning and a `support` field in the operator API.
- `/healthz` reports `degraded` when video stalls, and `/metrics` is new.
- `onvif.log_calls` logs every SOAP call with its user, peer and PTZ source.
- Logs are also written to a rotating, redacted file in the state volume,
  `<state_dir>/logs/osprey.log`, three files of 5 MiB.
- A move that the camera refuses is reported as refused, so a patrol never
  counts a move that did not happen.

### Fixes
- An autotracker can take the camera from a patrol that is still moving. The
  first tracking command of a detection was refused with "the camera is
  already moving", so tracking never started.
- A wedged PTZ channel is recovered. Osprey recycles the `ptz1` channel and
  keeps presets across restarts. When recovery gives up, it asks the camera to
  reboot, at most twice an hour, and recovery retries back off.
- Continuous moves chain field of view steps and no longer reverse on Stop.
- ONVIF authentication fails closed when only part of it is configured, and the
  state kept against replayed requests is bounded. Stalled oversized requests
  get an answer.
- RTSP sessions hold up against stalls, slow clients and RTCP. The digest nonce
  state is bounded and its validation is tighter. A track that is armed but
  that the camera has not started yet answers 503 with `Retry-After`.
- Adoption disarms every track, not only the last one.
- The setup endpoint closes once setup is complete, and the WebSocket closes
  cleanly.
- The console manages presets inline on the Live tab and shows PTZ watchdog
  health.
- The Proxmox installer writes a host networking Compose file with the camera
  ports.
- A pre-release no longer moves the `latest` Home Assistant image tag, so an
  existing install never pulls a beta.

### Packaging
- The Home Assistant add-on follows the 2026.11 wording and is now called an
  app.
- The npm package `@osprey-ptz/cli`, a Chocolatey package and a Homebrew
  formula ship launchers that run `doctor`, `probe` and `protect-guard`
  against the release image. They do not run the controller.
- Issue forms for bug reports and camera model support.
- The release workflow accepts PEP 440 pre-release tags and publishes them as
  pre-releases.

### Docs
- New pages for the sentry patrol and its MQTT commands, the Protect dual
  adoption setup, separate RTSP authentication, running Frigate with Home
  Assistant, and package installation. NOTICE and the license files credit the
  original authors.
