# Osprey Home Assistant app

Home Assistant 2026.11 renamed "add-ons" to **apps**. The manifest, the
`add-on` directory name and the Supervisor API are unchanged; only the wording
in the interface differs. Everything below uses the current names, with the
old ones in brackets where it helps.

Osprey bridges a UniFi G5 PTZ camera to an existing Frigate installation
through ONVIF and RTSP. It does not bundle, install, or manage Frigate.

## Install

1. Open **Settings → Apps → App store** (before Home Assistant 2026: **Add-ons → Add-on store**).
2. Open the three-dot menu, choose **Repositories**, and add
   `https://github.com/grayslawson/osprey-ha-apps`. This is the separate public app
   repository built from [`packaging/homeassistant-repo`](../../../packaging/homeassistant-repo/README.md).
   The main Osprey repository URL does not work here: Home Assistant needs
   `repository.yaml` at the repository root.
3. Install **Osprey**, then set `advertised_host` to the Home Assistant host
   address reachable by the camera. Optionally set `camera_ip` and
   `camera_mac` as identity guards.
4. Start the app and use **Open Web UI** (Home Assistant ingress is enabled).

### Why not the Marketplace

Home Assistant 2026.11 folded HACS into Core as **Settings → Marketplace**.
The Marketplace distributes integrations, dashboard cards, themes and
templates — it never distributed apps, and it does not now. Osprey is
installed from its own app repository through the **Repositories** menu, as
above, and the Marketplace change does not affect it.

Two things did change and are worth knowing:

- Dashboard resources moved from `/hacsfiles/` to `/local/community/`.
- Marketplace projects must live in a public GitHub repository, and adding a
  custom repository needs a GitHub device sign-in. The app repository has the
  same public-repository requirement.

The manifest declares `amd64` and `aarch64`; each needs a matching GHCR image
before that architecture is supported. The public release still
requires the GitHub Actions image build, GHCR package, and Home Assistant
Builder validation to be enabled.

## Local build

The Dockerfile uses the repository root as its build context because it needs
both `osprey/` and `pyunifiwire/`:

```sh
docker build -f homeassistant/addon/osprey/Dockerfile -t osprey-addon:local .
```

Home Assistant OS does not build a sibling-source checkout automatically. A
local add-on copy must include those source trees, or use a prebuilt image.
Publish versioned images; do not use `latest` for production.

## Network and security

`host_network: true` lets the camera reach the advertised ONVIF, RTSP, and
callback addresses on the Home Assistant host. Restrict TCP 8000 and the
configured camera-facing ports with the host firewall or a dedicated VLAN.
Never forward them to the Internet.

The bridge currently has no independent authentication on direct LAN
endpoints. Home Assistant ingress protects access through the HA account, but
direct host-network ports remain trusted-LAN services. Independent Osprey
authentication is required before untrusted or remote deployment.

## Frigate and release checks

Configure the existing Frigate instance with the ONVIF endpoint and RTSP stream
shown by the Osprey operator UI. Keep recording and retention in Frigate. Test
zones, pan, tilt, zoom, tracking, and safe-stop before unattended tracking.

Before release, run the Home Assistant add-on builder for every declared
architecture and verify ingress, `/data` persistence, upgrade, and reinstall.

Built on [Cuckoo](https://github.com/rjmotion/cuckoo), originally published by
rjmotion and contributors.

## How the pieces connect

Osprey is the camera-facing adapter; Frigate remains the recorder and detector. They may
both be Supervisor add-ons, or Frigate may run in Docker on another host. Enter the Frigate
HTTP/API URL in the Osprey operator console; it does not need to share a Docker network.

```mermaid
flowchart LR
  C[Ubiquiti G5 PTZ] -->|ONVIF + RTSP| O[Osprey add-on]
  O -->|RTSP stream| F[Frigate\nHA add-on or external container]
  U[HA user] -->|Ingress| O
  F -->|events/detection| O
```

### Frigate setup

Create the camera in Frigate using the RTSP URL and credentials shown by Osprey. For an HA
add-on use its Supervisor hostname/port (or host LAN address); for an external container
use its LAN DNS name/address. Do not use `localhost` unless both share a network namespace.
Keep retention, recording, detectors and zones configured in Frigate.

### Options, networking and security

`advertised_host` is mandatory: the HA host/LAN address used by camera callbacks and RTSP.
`camera_ip` and `camera_mac` are optional identity guards. `onvif_port` and `rtsp_port` must
be unused and reachable from the camera VLAN. The console is port 8000 through HA ingress;
host networking also publishes declared ports. Keep 7442/7444/7550 on the trusted LAN.

Ingress authenticates the console with Home Assistant, but direct host-network endpoints
are not independently authenticated. Firewall them to camera/Frigate VLANs, never
port-forward them, use a least-privilege HA account, and rotate camera credentials. A
deployment outside HA needs TLS and independent authentication.

### Release and validation

The app repository is a separate public Git repository with `repository.yaml` at its
root, built from [`packaging/homeassistant-repo`](../../../packaging/homeassistant-repo/README.md).
The Dockerfile builds from the Osprey repository root to copy `osprey/` and
`pyunifiwire/`. Publish immutable per-architecture GHCR images, update
`version`, run `homeassistant/validate.sh` and the
Home Assistant Add-on Builder for every declared architecture. Test clean install/upgrade,
ingress, `/data` persistence, discovery, Frigate ingest, PTZ actions, tracking and safe-stop.
`docker build -f homeassistant/addon/osprey/Dockerfile .` is only a local smoke test.

The manifest pins the image to the add-on version
(`ghcr.io/grayslawson/{arch}-osprey-addon:<version>`), so an installation
never runs an image from a different release. `homeassistant/validate.sh`
checks that the manifest version and the image tag name the same version as
`VERSION`, and the app repository is refreshed by a scheduled workflow that
mirrors this add-on with `scripts/sync-ha-apps.sh`.
