# Installation and distribution

Osprey is a Python controller for a camera already managed on the LAN. It does not install, configure, or run Frigate. The supported release artifact is `ghcr.io/grayslawson/osprey`.

## Docker or rootless Podman

Set `OSPREY_HOST` to the address the camera can reach and `OSPREY_BIND` to the
local interface on which Frigate should connect. The release image accepts a
single camera without a config file; mount a `cuckoo.json` file when you want
an explicit camera allow-list or multiple cameras:

```sh
export OSPREY_HOST=192.0.2.10
export OSPREY_BIND=192.0.2.10
export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:vX.Y.Z
docker compose -f compose.release.yaml up -d
# rootless Podman:
podman compose -f compose.release.yaml up -d
```

Images target `linux/amd64` and `linux/arm64`; keep camera-facing ports on a
trusted LAN. Rootless Podman may require host networking or explicit port
permissions for a camera VLAN. For multiple cameras, create `cuckoo.json` using
the [multi-camera schema](multi-camera.md), then add this bind mount under the
service in a local Compose override:

```yaml
services:
  osprey:
    volumes:
      - ./cuckoo.json:/workspace/cuckoo/cuckoo.json:ro
```

The operator console is served on the ONVIF port (8000 by default). Put it
behind HTTPS and configure admin authentication before exposing it beyond the
trusted LAN.

## Proxmox

Use a small Debian/Ubuntu VM or unprivileged LXC with Docker/Podman and this Compose file. Give it a bridged LAN interface able to reach the camera and existing Frigate, and expose only ONVIF/RTSP to the trusted network. Do not run Frigate in this container. A VM is the fallback if LXC networking or ffmpeg behavior is restricted.

For a repeatable guest setup, copy `scripts/proxmox-install.sh` into the guest and first inspect the plan:

```sh
sh proxmox-install.sh --host 192.0.2.10 --runtime podman
```

The helper validates amd64/arm64 and refuses to overwrite an existing state file. Add `--apply` only after reviewing the printed values; it may install the selected runtime with `apt-get`, write `/var/lib/osprey/compose.yaml`, and start the pinned image. Use `--image ghcr.io/grayslawson/osprey:vX.Y.Z` for releases. It never creates or modifies a Proxmox VM/LXC itself and contains no camera credentials.

## Home Assistant

An alpha add-on scaffold is in [`homeassistant/addon/osprey`](../homeassistant/addon/osprey/README.md). It is not in the official store: per-architecture images must be published and tested with the Home Assistant add-on builder first. HA still uses an external Frigate instance.

## Updates and rollback

Prefer approved, pinned releases rather than `latest`. Inspect first with `scripts/osprey-update.sh --file /var/lib/osprey/compose.yaml --image ghcr.io/grayslawson/osprey:vX.Y.Z`; add `--apply` only after review. Rollback uses the same command with the previous pinned image. Scheduled updates should be operator-owned and never follow `latest` automatically.

## Other package formats

npm, Homebrew, Chocolatey, and generic binaries are intentionally not shipped: Osprey is a network service, not a desktop CLI. A Nix package/module can be added when a maintainer owns service sandboxing and ffmpeg/ONVIF dependency updates.
