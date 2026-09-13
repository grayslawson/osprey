# Osprey Home Assistant add-on

This add-on runs the Osprey Cuckoo ONVIF/RTSP bridge and operator console under
Home Assistant OS, Supervised, or a compatible Home Assistant installation.

## Installation status

This is an alpha scaffold. It becomes installable from Supervisor after the
repository owner replaces `REPLACE_ME` in `config.yaml`, publishes the image
names declared there for each supported architecture, and adds this directory
to a Home Assistant add-on repository. Until then, build it with the repository
root as Docker context (see the Dockerfile comment) and install it as a local
add-on for development.

## Options

`advertised_host` is required and must be an address the camera can reach. Set
`camera_ip` and `camera_mac` when you want the optional identity checks. Osprey
uses host networking because the camera must open callback/control connections
back to the host; this is required for physical PTZ and means network isolation
must be provided by the host firewall/VLAN.

The operator console is available through the HA sidebar (ingress) or the
configured port 8000. The current alpha has no user authentication. Do not
publish it beyond a trusted LAN. Frigate is not bundled; configure Frigate
separately and point its ONVIF/RTSP camera at the add-on host.

## Current limitations

- Physical camera adoption/handoff and UniFi Protect credentials are not
  automated by this add-on.
- The image is currently amd64/aarch64/armv7 metadata; each architecture needs
  an actual published image and validation before release.
- Supervisor cannot build this Dockerfile from a sibling-source checkout. A CI
  release must build from the Osprey repository root and publish immutable,
  versioned images (or vendor a generated add-on source bundle).
- Upgrade/rollback, authentication, and Frigate lifecycle controls remain
  product work. State is retained under `/data/osprey`.

Osprey is built on [Cuckoo](https://github.com/rjmotion/cuckoo), originally
published by rjmotion and contributors.
