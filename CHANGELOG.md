# Changelog

Every release of Osprey, newest first. Each section is generated from the
Conventional Commit titles merged for that release, and it is also the body
of the GitHub release. See cliff.toml and CONTRIBUTING.md. The sections for
0.9.0b1 to 0.9.0b3 were written by hand, before the notes came from git-cliff,
and the 0.9.0b4 to 0.9.0b6 sections were rewritten by hand from the git-cliff output.

## [0.9.0b6] - 2026-11-24

### Assist
- Survey, a new Assist task, sweeps the camera across its whole pan and tilt
  range, stitches a day or night panorama, labels the regions it sees and
  scores how interesting each one is (using Frigate event counts when Frigate
  is connected). From that it proposes presets, a patrol route, night views and
  Frigate zones. Applying them follows the autonomy level and can be undone.
  See `docs/survey.md`, which also defines the shared scene-layer interfaces.
- Three new tasks, `quality.glare`, `quality.blur` and `quality.action`, each
  ask one decision model one question, with its own threshold and a fallback
  chain. The defaults come from a bake-off on real camera frames. Glare and
  blur answers overrule the local rules; exposure and noise stay with the
  rules. The judge scores before and after pictures with the same model.
  Pictures are still sent only when both the provider and the task allow it.
- Providers take an `extra_body` key, sent with every request, for example to
  ask a gateway not to keep data.

### Controller
- The state document carries `ptz.fov`, the camera's field of view.
- The Assist token has a new `patrol` scope, on by default, for editing patrol
  points and deleting presets.

## [0.9.0b5] - 2026-11-24

### Assist
- Osprey Assist is a new, optional helper that watches the camera's picture
  and fixes what it can: glare, blur, a picture that is too dark or too bright,
  and noise. It runs as a separate process from the same image (`osprey assist
  run`), it is off until you turn it on, and the controller does not depend on
  it. It asks before it changes anything, unless you set a capability to `act`
  or `auto`, and it undoes a change that does not help. See `docs/assist.md`.
- For night glare, where infrared light bounces off a wall near the camera,
  Assist looks for a nearby view that avoids it and suggests saving it as the
  patrol's night view.
- Approve suggestions in the console's new **Assist** tab, or in Home Assistant
  over MQTT. Assist adds a device with discovery, and can also send a ntfy
  message.
- A model can help decide, per task: rules, a model, or both. Assist speaks
  OpenAI-compatible chat and decision-model endpoints. A picture is sent only
  when both the provider and the task allow it. `osprey assist eval` scores a
  model on frames you have labelled.
- The image now includes Pillow, which only Assist uses.

### Controller
- The state document, the `ptz` event and the MQTT `state/ptz` message carry
  `scene`, `DAY` or `NIGHT`, as the camera reports it.
- The console's patrol tab can now set a night view for each position.
- The API can pause the patrol for up to five minutes and change how long it
  stays at a position for a while. Both expire on their own and neither is
  saved.
- A bearer token for Assist, switched on with `assist.enabled` or
  `OSPREY_ASSIST_TOKEN_FILE`, limited to a short list of routes and image
  settings. Nothing is open by default.
- The architecture notes now say which moves take the movement lease: the patrol
  and MQTT do, ONVIF and the console do not.

## [0.9.0b4] - 2026-11-24

### Setup
- Setting up now takes three steps: start Osprey with one command, open the
  link it prints in its log, and follow the wizard. The release Compose file
  needs no settings, because its image defaults to the release it came with.
  On first start the wizard opens by itself and fills in the host's LAN
  address. The link it logs expires after an hour, and the next request logs a
  new one. After you save, Osprey restarts itself.
- The wizard finds the camera. It sends the UniFi discovery probe on UDP 10001
  and lists each camera with its model, its firmware and whether that firmware
  has been tested. If UniFi Protect still manages the camera, the wizard shows
  how to remove and reset it, with a button to scan again. Then it adopts the
  camera, which used to need a `git clone` and `manage.sh`. `osprey adopt
  --camera IP` does the same from the command line.
- Once the camera is adopted, the wizard gives you a Frigate camera block with
  the address, ports and passwords filled in. It can also add the block to
  Frigate for you through Frigate's config API. You see a diff first, the old
  config is backed up to the state directory, and Osprey refuses if the config
  changed after the preview or already has a camera of the same name.
- The wizard ends with a check of the camera adoption, the video, ONVIF at the
  advertised address with Frigate's login, and the Frigate connection. Each
  failed check links to its fix in the troubleshooting guide.

### Security
- New installs are secure by default. ONVIF and RTSP require a login, and
  Osprey generates the credentials, stores them with mode 0600 in the state
  directory and logs them once. The camera ports accept only the adopted
  camera's address. An existing install is recorded as `legacy` and behaves as
  before, with a startup warning that recommends turning auth on. Set
  `OSPREY_SECURITY=secure` or `legacy` to choose. `OSPREY_ONVIF_AUTH=disabled`
  and `OSPREY_RTSP_AUTH=disabled` turn off one service at a time.
- New installs get the ONVIF user `frigate`, which `ptz.autotrack_users`
  already expects, so Frigate's moves count as autotracking with no config
  change. Credentials generated before this release keep the user `osprey`,
  and existing Frigate configs keep working.
- The Home Assistant app has options for the ONVIF and RTSP logins and for the
  security mode (`auto`, `secure` or `legacy`). Its base image is pinned by
  digest.
- `osprey protect-guard` verifies the console's TLS certificate by default.
  Use `--ca-file`, or `--pin-file` to trust the certificate it sees first.
  `--insecure` turns the check off. Its missing `sys` import is fixed too.
- The camera management command reads the camera password from
  `--password-file`, `OSPREY_CAMERA_PASSWORD` or a prompt. `--password` still
  works but warns, because the password shows up in `ps` and shell history.
- The control key and its PEM file are created with mode 0600, rather than
  being tightened after they're written.

### Camera and patrol
- Osprey sets the camera's ISP `hdr` to 1 every time PTZ connects
  (`ptz.isp_baseline`, default `{"hdr": 1}`; `{}` turns it off). On our G5 PTZ,
  Protect had left `hdr` at 2 and the image stayed soft at the preset zoom.
  With `hdr` at 1 both patrol presets were sharp. The camera won't go back to
  `hdr` 2, so we couldn't prove that this was the cause. `POST
  /api/v1/cameras/<id>/isp` passes other ISP settings through.
- A patrol point can have a `night_preset`. While the camera reports night,
  the patrol visits that preset instead, and falls back to the normal one if
  the night preset is missing or refused. A scene change has to hold for 120
  seconds before the patrol switches, so a flickering scene can't keep moving
  the head. We added it because the camera's own IR lit up a structure next to
  the camera and washed out a third of one view. The console keeps the setting
  when you edit a point, but can't set it yet.
- Osprey keeps a list of tested camera firmware. Today that's 5.3.95. Other
  firmware still works, but logs a warning when the camera is ready and shows
  up as a warning in the health checks and as `camera.firmware_status` in the
  state API.

### Other changes
- `osprey --version` and `osprey version` print the version.
- The container health check counts "waiting for a camera" and an unfinished
  first setup as healthy.
- The camera IP is optional in the wizard.
- A locked operator console is now a warning that says what to do, not an
  error.
- `osprey-update.sh --latest` works while every release is still a beta.

### Docs
- New guides for troubleshooting, upgrading and uninstalling, plus a FAQ and
  API and configuration references.
- The README is rewritten around the new quick start, and the security
  guide covers secure mode, the generated credentials and the camera address.
- NOTICE credits Finch. The package descriptions and the Home Assistant app
  say Osprey isn't affiliated with Ubiquiti.

### Not tested yet
- Camera discovery and adoption in the wizard haven't been tried on a real
  camera yet. They follow the measured protocol notes, and the whole wizard
  ran end to end in podman against a fake G5 PTZ and a fake Frigate. The
  managed flag on a factory reset G5 and the adoption call are the parts
  only real hardware can confirm.

Full list of changes: [v0.9.0b3...v0.9.0b4](https://github.com/grayslawson/osprey/compare/v0.9.0b3...v0.9.0b4)

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
