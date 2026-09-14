# Osprey Home Assistant add-on

Osprey bridges a Ubiquiti G5 PTZ camera to an existing Frigate installation
through ONVIF and RTSP. It does not bundle, install, or manage Frigate.

## Install

1. Open **Settings → Add-ons → Add-on store**.
2. Open the three-dot menu, choose **Repositories**, and add
   `https://github.com/grayslawson/osprey`.
3. Install **Osprey**, then set `advertised_host` to the Home Assistant host
   address reachable by the camera. Optionally set `camera_ip` and
   `camera_mac` as identity guards.
4. Start the add-on and use **Open Web UI** (Home Assistant ingress is enabled).

The manifest declares `amd64` and `aarch64`; each needs a matching GHCR image
before that architecture is supported. The public release still
requires the GitHub Actions image build, GHCR package, and Home Assistant
Builder validation to be enabled.

## Local build

The Dockerfile uses the repository root as its build context because it needs
both `cuckoo/` and `pyunifiwire/`:

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
