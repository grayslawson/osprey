<p align="center">
  <img src="assets/osprey-header.png" alt="An osprey in flight" width="900">
</p>

<h1 align="center">Osprey</h1>

<p align="center">
  Use a UniFi G5 PTZ camera with Frigate autotracking, without UniFi Protect
  controlling the camera.
</p>

<p align="center">
  <a href="https://github.com/grayslawson/osprey/releases"><img alt="Releases" src="https://img.shields.io/github/v/release/grayslawson/osprey?include_prereleases&style=flat-square"></a>
  <a href="https://github.com/grayslawson/osprey/actions/workflows/test.yml"><img alt="Tests" src="https://img.shields.io/github/actions/workflow/status/grayslawson/osprey/test.yml?branch=main&label=tests&style=flat-square"></a>
  <img alt="Stage: beta" src="https://img.shields.io/badge/stage-beta-b7791f?style=flat-square">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-2f6f54?style=flat-square">
</p>

Frigate can only autotrack a PTZ camera that speaks ONVIF. A UniFi PTZ camera
doesn't: it talks a private protocol to its UniFi controller. Osprey plays the
controller. The camera connects to Osprey, and Osprey serves its video as RTSP
and its pan, tilt and zoom as a standard ONVIF device. Frigate stays the
detector, recorder and autotracker. Osprey doesn't install, configure or run
Frigate.

> [!WARNING]
> Osprey replaces UniFi Protect as the camera's controller. To adopt a camera
> you remove it from Protect and factory-reset it, and Protect no longer
> manages it natively. You can [go back](docs/uninstall.md), or keep Protect
> recording through a [media-only ONVIF device](docs/protect-dual-adoption.md).
> This is beta software, and a camera firmware update can break it. Keep
> Osprey's ports on a trusted LAN or VLAN and never forward them to the
> Internet.

```mermaid
flowchart LR
  camera["UniFi G5 PTZ"] -- "UniFi protocol: control and video" --> osprey["Osprey"]
  osprey -- "RTSP video" --> frigate["Frigate"]
  frigate -- "ONVIF PTZ commands" --> osprey
  osprey -. "media-only ONVIF, optional" .-> protect["UniFi Protect"]
```

<!-- Screenshot placeholder: the console's Live tab, saved as docs/images/console-live.png. -->

## What you get

- RTSP restreams of the camera's three encoder tracks, H.264 or H.265 per
  track. Osprey doesn't transcode.
- ONVIF PTZ, presets and events, including what Frigate autotracking needs.
- A browser console with live view, presets, patrol, settings, logs and health,
  behind a password.
- A first-run setup wizard.
- [Sentry patrol](docs/sentry.md): an idle tour of presets that steps aside for
  autotracking and for a person.
- A [media-only ONVIF device](docs/protect-dual-adoption.md) on its own port,
  so UniFi Protect can record the camera alongside Frigate.
- [`osprey protect-guard`](docs/protect-guard.md), which checks for or creates
  the UniFi firewall rule that keeps Protect from adopting the camera itself.
- Optional [MQTT](docs/mqtt.md) commands and state.
- `osprey doctor` and [`osprey probe`](docs/camera-models.md) for diagnostics,
  and `/healthz` and `/metrics` for [monitoring](docs/ptz-health.md#health-endpoint).

## Supported hardware

| Camera | Status |
|---|---|
| UniFi G5 PTZ | Supported |
| Other UniFi PTZ cameras | Experimental. They adopt and stream, and PTZ uses the ranges the camera reports. See [camera models](docs/camera-models.md). |

Tested G5 PTZ firmware: 5.3.95. Osprey logs a warning and flags the Health tab
on any other version, because a firmware update can change the protocol. Test
tracking again after one.

Osprey runs as a container for `linux/amd64` and `linux/arm64`, as a Home
Assistant app on `amd64` and `aarch64`, as a NixOS service, or as a Python 3.13
wheel. It works with an existing Frigate. Osprey's own tests don't run Frigate,
so check Frigate's [autotracking requirements](https://docs.frigate.video/configuration/autotracking).

## Requirements

- A Linux host with a fixed LAN address that the camera and Frigate can both
  reach. The container uses host networking, so Docker Desktop on macOS and
  Windows won't work.
- These ports open between the camera, Osprey and your clients:

  | Port | Protocol | Used by |
  |---|---|---|
  | 7442, 7444, 7550 | TCP | The camera, for control, snapshots and video |
  | 8000 | TCP | ONVIF, the console, `/healthz`, `/metrics` |
  | 8554 | TCP | RTSP |
  | 3702 | UDP | WS-Discovery |

- A camera that is factory fresh. Its login is `ubnt`/`ubnt` until a
  controller adopts it, so it has to be removed from Protect and reset first.
- A machine with `git` and Python 3 that can reach the camera over HTTPS, to run
  `osprey/manage.sh` once.

## Quick start

1. Start Osprey. Pin a release tag, never `latest`:

   ```sh
   curl -fsSLO https://github.com/grayslawson/osprey/releases/download/v0.9.0b3/compose.release.yaml

   export OSPREY_HOST=192.0.2.10   # this host's LAN address, as the camera sees it
   export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:0.9.0b3
   export OSPREY_SETUP=1           # open the setup wizard

   docker compose -f compose.release.yaml up -d
   docker compose -f compose.release.yaml logs osprey | grep 'setup token'
   ```

2. Open `http://192.0.2.10:8000/setup`, enter the token from the log, and fill
   in the camera's MAC and name, the Frigate URL and an admin password. The
   wizard shows the generated RTSP and ONVIF passwords once, so save them. Then
   restart:

   ```sh
   docker compose -f compose.release.yaml restart osprey
   ```

3. Remove the camera from Protect and factory-reset it, so it is back on the
   default `ubnt`/`ubnt` login.

4. Point the camera at Osprey. The script asks for the camera's password; on a
   reset camera, type `ubnt`:

   ```sh
   git clone https://github.com/grayslawson/osprey.git && cd osprey
   ./osprey/manage.sh --camera 192.0.2.20 --user ubnt --host 192.0.2.10
   ```

   The console's Health tab shows the adoption.

5. Add the camera to your Frigate, with the RTSP password the wizard showed:

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

   The [Frigate guide](docs/frigate-integration.md) covers ONVIF credentials,
   autotracking, calibration and presets.

6. Check the setup with `osprey doctor`; see
   [verify a deployment](docs/installation.md#verify-a-deployment).

If something doesn't work, start with [troubleshooting](docs/troubleshooting.md).

## Other ways to install

Details and status for each are in [installation](docs/installation.md).

- Home Assistant app: add `https://github.com/grayslawson/osprey-ha-apps` as an
  app repository.
- NixOS: the flake exports a package and a module. See [NixOS](docs/nixos.md).
- Proxmox: a Docker or Podman guest, with an installer script.
- Python wheel: attached to each GitHub release. It needs Python 3.13.
- Homebrew, Chocolatey and npm launchers run `osprey doctor` and the other
  read-only tools. They don't run the controller.

## Using it with UniFi Protect

Protect and Osprey can't both control the same camera. You can keep Protect
recording through the [media-only ONVIF device](docs/protect-dual-adoption.md),
and block Protect from adopting the physical camera with
[Protect guard](docs/protect-guard.md). To give the camera back to Protect
entirely, follow [uninstall and return to Protect](docs/uninstall.md).

## Documentation

Start at the [documentation index](docs/README.md). The most used pages are
[troubleshooting](docs/troubleshooting.md), the [FAQ](docs/faq.md),
[configuration](docs/configuration.md), the [API reference](docs/api.md) and
[upgrading](docs/upgrading.md). Security guidance is in
[`docs/security.md`](docs/security.md), and vulnerability reports go through
[`SECURITY.md`](SECURITY.md).

## Names

Osprey grew out of three projects by the flock contributors: [Cuckoo](https://github.com/rjmotion/cuckoo), a controller that
isn't there, which Osprey carries forward and renames;
[pyunifiwire](https://github.com/rjmotion/pyunifiwire), the wire library that
Osprey bundles from `pyunifiwire/`; and [Finch](https://github.com/rjmotion/finch),
a synthetic camera for testing. Old file names such as `cuckoo.json` are still
read; see [upgrading](docs/upgrading.md).

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md), and run
`make check` before you open a pull request. Use the issue forms for bugs and
camera model support. Leave credentials, private addresses, MAC addresses and
recordings out of every report.

## License

MIT. See [`LICENSE`](LICENSE). Attribution for the projects above is in
[`NOTICE`](NOTICE).

Osprey is an independent project. It isn't affiliated with, endorsed by or
supported by Ubiquiti Inc. UniFi and UniFi Protect are trademarks of Ubiquiti
Inc., used here to name the hardware Osprey works with.
