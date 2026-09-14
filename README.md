<p align="center">
  <img src="assets/osprey-header.png" alt="An osprey in flight" width="900">
</p>

<h1 align="center">Osprey</h1>

<p align="center">
  A local controller that brings Ubiquiti G5 PTZ cameras to Frigate.
</p>

<p align="center">
  <a href="https://github.com/grayslawson/osprey/releases"><img alt="Releases" src="https://img.shields.io/github/v/release/grayslawson/osprey?style=flat-square"></a>
  <img alt="Stage: alpha" src="https://img.shields.io/badge/stage-alpha-b7791f?style=flat-square">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-2f6f54?style=flat-square">
  <img alt="Local-first video" src="https://img.shields.io/badge/video-local--first-304b61?style=flat-square">
</p>

Osprey moves a UniFi G5 PTZ camera out of Protect’s unreliable tracking path and
into an existing Frigate installation. Frigate remains the detector, recorder,
and tracking engine. Osprey owns the camera connection and exposes standard
ONVIF and RTSP endpoints for Frigate and other clients.

> [!WARNING]
> Osprey is alpha software. Keep camera-facing ports on a trusted LAN or VLAN,
> never forward them to the Internet, and supervise adoption and tracking tests.
> Osprey does not install, configure, or run Frigate.

## What Osprey provides

- Frigate integration through ONVIF PTZ and RTSP media endpoints.
- Pan, tilt, and zoom control with bounded movement, arbitration, and presets.
- Multiple named positions, including a configurable home position.
- A browser operator console with live video, PTZ controls, settings, metrics,
  and recent-log warnings.
- Optional MQTT commands and control events with retained-command protection.
- Multiple cameras in one controller, with camera-specific ONVIF personas.
- A read-only ONVIF persona for NVR/Protect experiments. It republishes media
  without exposing PTZ controls; Protect firmware compatibility varies.
- Docker/Podman, Proxmox, NixOS, and Home Assistant add-on deployment paths.

## How it fits together

```text
                    ONVIF/PTZ
                 ┌──────────────┐
                 │   Frigate    │
                 │ detector +   │
                 │ autotracker  │
                 └──────┬───────┘
                        │ RTSP + PTZ
┌─────────────────┐     ▼     ┌──────────────────┐
│ UniFi G5 PTZ    │◀─────────▶│ Osprey           │
│ camera          │  private  │ controller       │
└─────────────────┘  camera   └────────┬─────────┘
                                       │ RTSP
                                       ▼
                                NVR, VLC, Protect
```

The physical camera has one native UniFi controller relationship. Osprey owns
that relationship; Protect cannot natively adopt the same physical camera at
the same time. The optional read-only ONVIF persona is a separate, generic
camera backed by Osprey’s republished stream. See
[`docs/protect-dual-adoption.md`](docs/protect-dual-adoption.md).

## Quick start with Docker

The published image supports `linux/amd64` and `linux/arm64`. Pin a release tag
or digest in production:

```bash
git clone https://github.com/grayslawson/osprey.git
cd osprey

export OSPREY_HOST=192.0.2.10  # address reachable by the camera and clients
export OSPREY_BIND=192.0.2.10  # interface exposed to Frigate
export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:vX.Y.Z

docker compose -f compose.release.yaml up -d
```

The ONVIF/operator endpoint uses port `8000`; RTSP uses `8554`. Configure the
camera identity and any additional cameras in `cuckoo.json` as described in
[`docs/multi-camera.md`](docs/multi-camera.md). Keep credentials in a secret
manager or an untracked deployment file.

See [`docs/installation.md`](docs/installation.md) for rootless Podman,
Proxmox, NixOS, Home Assistant, updates, rollback, and development setup.

## Connect an existing Frigate installation

In Osprey’s operator console, open **Settings → Frigate** and enter the base URL
of the Frigate instance you already run, such as `http://frigate.local:5000`.
Osprey uses this URL for health feedback and links; it never starts or rewrites
Frigate.

Then add the Osprey stream to Frigate. The exact configuration depends on your
Frigate version, but the endpoints have this shape:

```yaml
cameras:
  g5_ptz:
    onvif:
      host: osprey.local
      port: 8000
    ffmpeg:
      inputs:
        - path: rtsp://osprey.local:8554/video2
          roles: [detect, record]
```

Keep object classes, zones, recording, retention, and Frigate’s zoom/autotrack
settings in Frigate. See the
[`docs/frigate-integration.md`](docs/frigate-integration.md) guide.

## Home Assistant and NixOS

The repository includes an alpha Home Assistant add-on and a declarative NixOS
package/module. Both run Osprey only; Frigate can be another Home Assistant
add-on or a service on another host.

- [Home Assistant add-on guide](homeassistant/addon/osprey/README.md)
- [NixOS package and module guide](docs/nixos.md)

## Security

- Keep control, ONVIF, RTSP, ingest, and snapshot ports on a trusted network.
- Set an admin password before exposing the browser console beyond that network.
- ONVIF UsernameToken authentication is opt-in while Frigate interoperability
  is being validated. Anonymous ONVIF allows reachable clients to issue PTZ
  commands, so use a firewall or VLAN when it is enabled.
- RTSP currently has no application-level password. Restrict it with bind
  addresses, firewall rules, or a trusted proxy.
- Keep camera, Protect, MQTT, admin, and Frigate credentials out of Git and
  issue reports. See [`docs/security.md`](docs/security.md).

## Stay current

Releases publish versioned multi-architecture images. Follow the repository’s
**Releases** page (or choose **Watch → Custom → Releases**) for notifications.
The [`scripts/osprey-update.sh`](scripts/osprey-update.sh) helper is check-only
by default; review the release and approve a pinned update with `--apply`.
Optional systemd units provide scheduled checks without forcing unattended
restarts. See [updates, approval, and rollback](docs/installation.md#updates-approval-and-rollback).

## Built on prior open source work

Osprey builds on the original [Cuckoo](https://github.com/rjmotion/cuckoo) and
[pyunifiwire](https://github.com/rjmotion/pyunifiwire) projects. Their authors’
protocol research and code are credited under the applicable license notices.

## Contributing

Run `./cuckoo/test.sh` and `./pyunifiwire/test.sh` before submitting changes.
When reporting an issue, include the Osprey version, host platform, camera
model, Frigate version, and a redacted reproducible log. Never include
credentials, private addresses, MAC addresses, or video from someone else’s
property.

## License

Osprey is available under the [MIT License](LICENSE). Upstream components keep
their own license notices.
