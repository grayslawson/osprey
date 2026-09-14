# Installation and distribution

Osprey is a Python controller for a camera already managed on the LAN. It does not install, configure, or run Frigate. The supported release artifact is `ghcr.io/grayslawson/osprey`.

## Docker or rootless Podman

Set `OSPREY_HOST` to the host address reachable by the camera, then run:

```sh
docker compose -f compose.release.yaml up -d
# rootless Podman:
podman compose -f compose.release.yaml up -d
```

Set `OSPREY_IMAGE=ghcr.io/grayslawson/osprey:vX.Y.Z` to pin a release. Images target `linux/amd64` and `linux/arm64`; keep camera-facing ports on a trusted LAN. Rootless Podman may require host networking or explicit port permissions for a camera VLAN.

## Proxmox

Use a small Debian/Ubuntu VM or unprivileged LXC with Docker/Podman and this Compose file. Give it a bridged LAN interface able to reach the camera and existing Frigate, and expose only ONVIF/RTSP to the trusted network. Do not run Frigate in this container. A VM is the fallback if LXC networking or ffmpeg behavior is restricted.

## Home Assistant

An alpha add-on scaffold is in [`homeassistant/addon/osprey`](../homeassistant/addon/osprey/README.md). It is not in the official store: per-architecture images must be published and tested with the Home Assistant add-on builder first. HA still uses an external Frigate instance.

## Other package formats

npm, Homebrew, Chocolatey, and generic binaries are intentionally not shipped: Osprey is a network service, not a desktop CLI. A Nix package/module can be added when a maintainer owns service sandboxing and ffmpeg/ONVIF dependency updates.
