# Configuration

Osprey reads settings from three places. A command-line flag beats the config
file, and the config file beats the built-in default. Secrets come from
environment variables or files, never from flags.

## Config file

`osprey.json` holds non-secret runtime settings. The container reads
`/var/lib/osprey/osprey.json`; elsewhere Osprey reads `osprey.json` in the
working directory, or the path in `--config` or `OSPREY_CONFIG_FILE`. A
missing default file means defaults. A missing `--config` file is an error.
An older `cuckoo.json` is read when `osprey.json` does not exist.

Every key is optional except `host`:

| Key | Default | Meaning |
|---|---|---|
| `host` | none | The LAN address the camera and clients reach Osprey on. Not a loopback or wildcard. |
| `bind` | `0.0.0.0` | Local address for the HTTP and ONVIF listeners. |
| `name` | `osprey` | Controller name shown to the camera and in discovery. |
| `cert` | `osprey.pem` | Control-channel certificate. Generated on first run. |
| `announce` | `true` | Multicast a WS-Discovery Hello. `false` only answers probes. The Home Assistant app turns it off by default. |
| `cameras` | `[]` | Allow-list of cameras. See [cameras](#cameras) and [multi-camera](multi-camera.md). |
| `tracks` | H.264 on `video1` to `video3` | Channel to codec: `h264`, `h265` or `mjpg`. A file that names one channel changes only that channel. The Home Assistant app defaults to `video1:h264,video2:h265`. |
| `frigate.url` | none | Frigate base URL for links and health. `http` or `https`, no credentials, query or fragment. |
| `ports.*` | see below | Listener ports. |
| `ptz.*` | see [PTZ](#ptz) | Speed, watchdog, presets and arbitration. |
| `onvif.log_calls` | `false` | Log every SOAP call at INFO. |
| `onvif.identity.*` | derived | Northbound device identity overrides. |
| `discovery.*` | see below | WS-Discovery interface, cadence and scopes. |
| `sentry.*` | off | Idle patrol. See [sentry](#sentry). |
| `mqtt.*` | off | MQTT bridge. See [MQTT](#mqtt). |

### Ports

| Key | Default | Used by |
|---|---|---|
| `ports.control` | 7442 | Camera control channel (TLS) |
| `ports.snapshot` | 7444 | Camera snapshot upload |
| `ports.ingest` | 7550 | Camera media |
| `ports.onvif` | 8000 | ONVIF, console, `/healthz`, `/metrics` |
| `ports.onvif_read_only` | off | [Media-only ONVIF device](protect-dual-adoption.md) |
| `ports.rtsp` | 8554 | RTSP |
| `ports.discovery` | 3702 (UDP) | WS-Discovery |

Each port also has a flag: `--control-port`, `--snapshot-port`,
`--ingest-port`, `--onvif-port`, `--onvif-read-only-port`, `--rtsp-port` and
`--discovery-port`.

### Cameras

`cameras` is optional. With no list, Osprey accepts any compatible camera.
With one, it rejects every camera whose MAC is not on it. Each entry is an
object:

| Key | Default | Meaning |
|---|---|---|
| `mac` | none, required | 12 hexadecimal digits. Colons and dashes are accepted and stripped. Must be unique. |
| `name` | the MAC | Display name, shown to ONVIF clients. |
| `ip` | none | The camera's address. When set, Osprey accepts media and snapshot uploads only from it. It is never the camera's identity; the MAC is. |
| `tracks` | none | An object that overrides `tracks` for this camera. |

### PTZ

The `ptz` block. An unknown key is an error, and every number is range checked
at start.

| Key | Default | Range | Meaning |
|---|---|---|---|
| `max_speed` | `1000` | 1 to 10000 | Top speed on the G5 scale. See [PTZ speed](#ptz-speed). |
| `auto_home` | `true` | boolean | Save the first confirmed position as `home` when no such preset exists. |
| `reply_timeout_sec` | `30.0` | 0.5 to 300 | Recycle the PTZ channel when the oldest position request has gone unanswered this long. |
| `min_silence_sec` | `10.0` | 0 to 300 | Silence required before the unanswered count triggers a recycle. |
| `max_unanswered` | `3` | 1 to 20 | Unanswered position requests that trigger a recycle. |
| `probe_interval_sec` | `2.0` | 0.1 to 60 | Position poll interval while the state is unknown. |
| `idle_probe_sec` | `60.0` | 0, or 5 to 3600 | Position poll interval on an idle, healthy channel. `0` turns it off. |
| `presets_file` | `null` | path | Where presets are saved. `null` means `osprey-presets.json` beside the certificate. |
| `manual_hold_sec` | `30.0` | 0 to 3600 | How long Frigate is held off after a person moves the camera. `0` turns arbitration off. |
| `autotrack_users` | `["frigate"]` | list of names | ONVIF users that count as autotrackers. Anonymous callers always do. |

[PTZ health](ptz-health.md) explains the watchdog.

### Sentry

The `sentry` block holds defaults that each camera inherits, plus a `cameras`
object keyed by camera MAC. Patrols are described in [sentry](sentry.md).

| Key | Default | Range | Meaning |
|---|---|---|---|
| `enabled` | `false` | boolean | Run the patrol. |
| `interval_sec` | `45` | 1 to 86400 | Seconds between moves. |
| `speed` | `1000` | 1 to 10000 | G5 speed for patrol moves. |
| `tracking_hold_sec` | `15` | 1 to 3600 | How long the patrol waits after the last autotracking command. |
| `cameras.<MAC>` | none | object | Per camera `enabled`, `interval_sec`, `speed`, `tracking_hold_sec` and `points`. |

A point is `{"name": "gate", "dwell_sec": 20}`. `name` is required and unique
per camera, and `dwell_sec` (1 to 86400) is optional. Changes made at runtime
are saved to `sentry.json` in the state directory, on top of this file.

### MQTT

MQTT is off until `mqtt.enabled` is `true`. See [MQTT](mqtt.md) for the
topics and for how to keep the password safe.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Connect to the broker. |
| `host` | none | Broker address. Required when enabled. |
| `port` | `1883`, or `8883` with `tls` | Broker port, 1 to 65535. |
| `tls` | `false` | Use TLS, verified against the system CA store. |
| `username`, `password` | none | Broker credentials. The password is the one secret kept in this file. |
| `base_topic` | `osprey` | Topic prefix, with no leading or trailing `/`. |
| `device` | `osprey` | Topic segment for this controller, with no `/`. |

### PTZ speed

ONVIF speeds map linearly onto the G5 scale from 1 to `ptz.max_speed`. A
request without a speed runs at `ptz.max_speed`. The default is `1000`, the
value Protect sends and the only one measured on hardware. It accepts 1 to
10000; measure anything above 1000 on your camera first.
`OSPREY_PTZ_MAX_SPEED` overrides the file.

### ONVIF identity

Osprey derives its own serial, MAC and hostname, and never reports the
camera's. Override them only to resolve a clash:

```json
{"onvif": {"identity": {"mac": "02:00:00:00:00:01", "hostname": "osprey-ptz"}}}
```

Keys: `manufacturer`, `model`, `serial`, `hardware_id` (1 to 64 printable
ASCII characters each), `mac` (unicast, and different from every camera MAC),
`hostname` (an RFC 1123 label) and `prefix_length` (default 24). The
[media persona](protect-dual-adoption.md) ignores these and derives its own.

### Discovery

| Key | Default | Meaning |
|---|---|---|
| `discovery.interface` | none | Network interface for multicast, such as `eth0`. |
| `discovery.address` | none | IPv4 address of that interface. |
| `discovery.hello_interval` | 300 | Seconds between Hellos. `0` sends one at startup only; otherwise 30 to 86400. |
| `discovery.scopes` | `[]` | Up to 16 extra ONVIF scope URIs. |

## Environment variables

### Runtime

| Variable | Meaning |
|---|---|
| `OSPREY_HOST` | Advertised host. Required by the container unless the file sets `host`. |
| `OSPREY_BIND` | Listener address. With the bridge Compose file, also the address ports are published on. |
| `OSPREY_CONFIG_FILE` | Config file path. |
| `OSPREY_STATE_DIR` | Writable state directory (container). Default `/var/lib/osprey`. |
| `OSPREY_CERT_FILE` | Control-channel certificate (container). |
| `OSPREY_SECRETS_FILE` | `KEY=value` secrets file. Default `osprey-secrets.env` next to the config. |
| `OSPREY_SETUP` | `1` opens the setup wizard while the config has no `host`. Same as `--setup`. |
| `OSPREY_ANNOUNCE` | `false` disables the WS-Discovery Hello (container). |
| `OSPREY_EXTRA_ARGS` | Extra controller flags, split on whitespace (container). |
| `OSPREY_PTZ_MAX_SPEED` | Overrides `ptz.max_speed`. |
| `OSPREY_PRESETS_FILE` | Overrides `ptz.presets_file`. |
| `OSPREY_FRIGATE_CONFIG_FILE` | Where the Frigate URL set in the console is kept. Without it the setting is lost on restart. |

### Authentication

| Variable | Meaning |
|---|---|
| `OSPREY_ADMIN_PASSWORD_HASH`, `OSPREY_ADMIN_PASSWORD_FILE` | Console password. Without one the console is locked. |
| `OSPREY_ADMIN_AUTH` | `disabled` opens the console without a password. Development only. |
| `OSPREY_COOKIE_SECURE` | `1` marks the session cookie `Secure` behind a proxy that sends no forwarding headers. |
| `OSPREY_TRUSTED_INGRESS_PEER` | Home Assistant ingress proxy address whose requests count as signed in. |
| `OSPREY_ONVIF_USERNAME`, `OSPREY_ONVIF_PASSWORD`, `OSPREY_ONVIF_PASSWORD_FILE` | Single ONVIF credential. |
| `OSPREY_ONVIF_USERS_FILE` | ONVIF users and roles. |
| `OSPREY_ONVIF_MAX_SKEW_SECONDS` | Accepted WS-Security clock skew, 30 to 3600. Default 300. |
| `OSPREY_RTSP_USERNAME`, `OSPREY_RTSP_PASSWORD`, `OSPREY_RTSP_PASSWORD_FILE` | RTSP Digest credential. |

Prefer the `_FILE` forms. See [authentication](authentication.md) and
[security](security.md).

### Tools

| Variable | Used by | Meaning |
|---|---|---|
| `OSPREY_CAMERA_IP`, `OSPREY_CALLBACK_URL`, `OSPREY_FRIGATE_URL` | `osprey doctor` | Defaults for `--camera`, `--callback` and `--frigate`. |
| `OSPREY_UNIFI_API_KEY_FILE` | `protect-guard`, `doctor` | UniFi Network API key file. |
| `OSPREY_CAMERA_PASSWORD` | `manage.sh` | The camera's management password, instead of a prompt. |
| `OSPREY_UNIFI_CONSOLE` | `doctor` | UniFi console URL for the guard check. |
| `OSPREY_HEALTH_HOST`, `OSPREY_HEALTH_PORT` | container health check | Where to probe `/healthz`. Defaults follow `OSPREY_BIND` and `ports.onvif`. |
| `OSPREY_SECRETS` | `osprey/handoff.sh` | Directory with Protect credentials. Default `~/.osprey/secrets`. |
| `OSPREY_IMAGE` | Compose files, npm launcher | Image to run. Required, and pin it to a release tag or digest. |
| `OSPREY_RUNTIME` | npm launcher | `docker` or `podman`. |
| `OSPREY_CAMERA_MAC` | `compose.physical.yaml` | Passed to `--expected-camera-mac`. |
| `OSPREY_GITHUB_REPOSITORY`, `OSPREY_RELEASE_API_URL` | `scripts/osprey-update.sh --latest` | Where to look up the newest release. |

`osprey/handoff.sh` has more `OSPREY_*` overrides, listed in its header. It is
a maintainer tool for moving a camera between controllers, not part of a
normal install.

## Flags

`osprey --help` lists every flag. Those without a config key:

| Flag | Meaning |
|---|---|
| `--setup` | Open the setup wizard. |
| `--no-announce` | Answer discovery probes without multicasting. Same as `announce: false`. |
| `--expected-camera-ip`, `--expected-camera-mac` | Reject a camera control connection from any other address or MAC. |
| `--dump FILE` | Write every camera message as JSON lines. Not redacted. |
| `--verbose` | Debug logging. |
