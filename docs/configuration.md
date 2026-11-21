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
| `announce` | `true` | Multicast a WS-Discovery Hello. `false` only answers probes. |
| `cameras` | `[]` | Allow-list of cameras. See [multi-camera](multi-camera.md). |
| `tracks` | H.264 on `video1`–`video3` | Channel to codec, `h264` or `h265`. |
| `frigate.url` | none | Frigate base URL for links and health. |
| `ports.*` | see below | Listener ports. |
| `ptz.*` | see [PTZ health](ptz-health.md) | Speed, watchdog, presets and arbitration. |
| `onvif.log_calls` | `false` | Log every SOAP call at INFO. |
| `onvif.identity.*` | derived | Northbound device identity overrides. |
| `discovery.*` | see below | WS-Discovery interface, cadence and scopes. |
| `sentry.*` | off | Idle patrol. See [sentry](sentry.md). |
| `mqtt.*` | off | MQTT bridge. See [MQTT](mqtt.md). |

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
| `OSPREY_UNIFI_CONSOLE` | `doctor` | UniFi console URL for the guard check. |
| `OSPREY_HEALTH_HOST`, `OSPREY_HEALTH_PORT` | container health check | Where to probe `/healthz`. Defaults follow `OSPREY_BIND` and `ports.onvif`. |
| `OSPREY_SECRETS` | `osprey/handoff.sh` | Directory with Protect credentials. Default `~/.osprey/secrets`. |

## Flags

`osprey --help` lists every flag. Those without a config key:

| Flag | Meaning |
|---|---|
| `--setup` | Open the setup wizard. |
| `--no-announce` | Answer discovery probes without multicasting. |
| `--expected-camera-ip`, `--expected-camera-mac` | Reject a camera control connection from any other address or MAC. |
| `--dump FILE` | Write every camera message as JSON lines. Not redacted. |
| `--verbose` | Debug logging. |
