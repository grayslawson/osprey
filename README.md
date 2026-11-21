<p align="center">
  <img src="assets/osprey-header.png" alt="An osprey in flight" width="900">
</p>

<h1 align="center">Osprey</h1>

<p align="center">
  A self-hosted controller that serves UniFi PTZ cameras to Frigate over ONVIF
  and RTSP.
</p>

<p align="center">
  <a href="https://github.com/grayslawson/osprey/releases"><img alt="Releases" src="https://img.shields.io/github/v/release/grayslawson/osprey?include_prereleases&style=flat-square"></a>
  <img alt="Stage: beta" src="https://img.shields.io/badge/stage-beta-b7791f?style=flat-square">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-2f6f54?style=flat-square">
</p>

Osprey takes the place of the UniFi controller for a PTZ camera. The camera
connects to Osprey, and Osprey serves its video as RTSP and its pan, tilt and
zoom as a standard ONVIF device. Frigate stays the detector, recorder and
autotracker. Osprey does not install, configure or run Frigate.

> [!WARNING]
> 0.9 is a beta. Keep Osprey's ports on a trusted LAN or VLAN, never forward
> them to the Internet, and watch the first adoption and tracking tests.

## What you get

- RTSP restreams of the camera's encoder channels, H.264 or H.265 per channel.
- ONVIF PTZ, presets and events, including Frigate autotracking.
- A browser console with live view, presets, patrol, settings, logs and health,
  behind a password with CSRF protection.
- A first-run setup wizard (`--setup` or `OSPREY_SETUP=1`).
- [Sentry patrol](docs/sentry.md): an idle tour of presets that steps aside for
  autotracking and for a person, and survives a restart.
- A [media-only ONVIF device](docs/protect-dual-adoption.md) on its own port,
  so UniFi Protect can record the camera alongside Frigate without moving it.
- [`osprey protect-guard`](docs/protect-guard.md), which checks for or creates
  the UniFi firewall rule that keeps Protect from adopting the camera itself.
- Optional [MQTT](docs/mqtt.md) commands and state.
- `osprey doctor` and [`osprey probe`](docs/camera-models.md) for diagnostics,
  and [`/healthz` and `/metrics`](docs/ptz-health.md#health-endpoint) for
  monitoring.

## Supported cameras

| Camera | Status |
|---|---|
| UniFi G5 PTZ | Supported |
| Other UniFi PTZ cameras | Experimental. They adopt and stream, and PTZ uses the ranges the camera reports. See [camera models](docs/camera-models.md). |

## Quick start

The release image runs on `linux/amd64` and `linux/arm64` with host
networking. Pin a release tag or digest, never `latest`:

```sh
curl -fsSLO https://github.com/grayslawson/osprey/releases/download/v0.9.0b2/compose.release.yaml

export OSPREY_HOST=192.0.2.10   # this host's LAN address, as the camera sees it
export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:0.9.0b2
export OSPREY_SETUP=1           # open the setup wizard

docker compose -f compose.release.yaml up -d
docker compose -f compose.release.yaml logs osprey | grep 'setup token'
```

Open `http://192.0.2.10:8000/setup`, enter the token from the log, and fill
in the camera's MAC and name, the Frigate URL and an admin password. The
wizard writes the configuration and `0600` secret files to the state volume
and shows generated RTSP and ONVIF passwords once. Save them, then restart:

```sh
docker compose -f compose.release.yaml restart osprey
```

The wizard closes itself once a configuration is saved, so `OSPREY_SETUP` can
stay set. Next, [point the camera at Osprey](docs/installation.md#point-the-camera-at-osprey)
and add it to your existing Frigate, using the RTSP password the wizard
showed:

```yaml
cameras:
  g5_ptz:
    ffmpeg:
      inputs:
        - path: rtsp://osprey:RTSP_PASSWORD@192.0.2.10:8554/video2
          roles: [detect, record]
    onvif:
      host: 192.0.2.10
      port: 8000
```

The [Frigate guide](docs/frigate-integration.md) covers credentials,
autotracking and presets.

Other ways to install: Home Assistant app, NixOS module, Proxmox LXC, PyPI,
Homebrew, Chocolatey and npm. See [installation](docs/installation.md).

## Documentation

Start at the [documentation index](docs/README.md). Security guidance is in
[`docs/security.md`](docs/security.md), and vulnerability reports go through
[`SECURITY.md`](SECURITY.md).

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md), and run
`make check` before you open a pull request. Use the issue forms for bugs and
camera model support. Leave credentials, private addresses, MAC addresses and
recordings out of every report.

## License

MIT. See [`LICENSE`](LICENSE). Osprey builds on the original
[Cuckoo](https://github.com/rjmotion/cuckoo) and
[pyunifiwire](https://github.com/rjmotion/pyunifiwire) projects; attribution
and the statement that Osprey is not affiliated with Ubiquiti Inc. are in
[`NOTICE`](NOTICE).
