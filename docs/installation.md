# Installation and distribution

Osprey is a controller for a Ubiquiti G5 PTZ camera. It does not install,
configure, or run Frigate. Bring an existing Frigate installation and connect
it to Osprey's ONVIF and RTSP endpoints.

The supported release image is `ghcr.io/grayslawson/osprey`, published for
`linux/amd64` and `linux/arm64`.

## Docker or rootless Podman

Set `OSPREY_HOST` to an address reachable by the camera and
`OSPREY_BIND` to the interface where Frigate should connect. Pin a release
tag or digest instead of using `latest`:

```sh
export OSPREY_HOST=192.0.2.10
export OSPREY_BIND=192.0.2.10
export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:vX.Y.Z
docker compose -f compose.release.yaml up -d
# Rootless Podman:
podman compose -f compose.release.yaml up -d
```

ONVIF and the operator console use port `8000`; RTSP uses `8554`. Keep
camera-facing ports on a trusted LAN. Rootless Podman may require host
networking or explicit port permissions for a camera VLAN.

For multiple cameras, create `cuckoo.json` using the
[multi-camera schema](multi-camera.md), then add this read-only bind mount in
a local Compose override:

```yaml
services:
  osprey:
    volumes:
      - ./cuckoo.json:/workspace/cuckoo/cuckoo.json:ro
```

Configure the browser admin password before exposing the console beyond the
trusted LAN. See [authentication](authentication.md) and [security](security.md).

## Proxmox

Use a small Debian/Ubuntu VM or an unprivileged LXC with Docker or Podman and
the release Compose file. Give it a bridged LAN interface that can reach the
camera and your existing Frigate host. Expose only the ONVIF/RTSP ports to the
trusted network; do not run Frigate in the Osprey container.

For a repeatable guest setup, copy [`scripts/proxmox-install.sh`](../scripts/proxmox-install.sh)
into the guest and inspect its plan first:

```sh
sh proxmox-install.sh --host 192.0.2.10 \
  --bind-address 192.0.2.10 \
  --image ghcr.io/grayslawson/osprey:vX.Y.Z --runtime podman
```

Add `--apply` only after reviewing the printed values. The bind address
defaults to loopback; set `--bind-address` to the guest's LAN address when the
camera or Frigate must reach Osprey. The helper requires an immutable image
tag or digest, can install the selected runtime with `apt-get`, writes the
Compose file under `/var/lib/osprey`, and starts only that service. It does not
create or modify a Proxmox VM/LXC and never contains camera credentials.

## Home Assistant

An alpha add-on is in [`homeassistant/addon/osprey`](../homeassistant/addon/osprey/README.md).
It runs Osprey with host networking and Home Assistant ingress. Frigate may be
another Home Assistant add-on or an external container/host; Osprey does not
manage it. Follow the add-on guide for custom repository installation and
architecture image requirements.

## NixOS

The repository includes a reproducible flake package and NixOS module. The
module runs Osprey as a DynamicUser systemd service and leaves camera
credentials, firewall policy, camera adoption, and Frigate configuration to
your NixOS configuration. See [`docs/nixos.md`](nixos.md).

## Updates, approval, and rollback

Osprey publishes a GitHub Release and a matching multi-architecture image for
each `vX.Y.Z` tag. Follow the repository's **Releases** page (or choose
**Watch → Custom → Releases**) to receive a notification when an update is
available. Pin the image in your environment file; do not use `latest` for a
production camera controller.

The update helper is safe by default. It validates the Compose file and prints
the exact image that would be used, but does not pull, restart, or modify any
service:

```sh
scripts/osprey-update.sh \
  --file /var/lib/osprey/compose.release.yaml \
  --env-file /etc/osprey/release.env \
  --image ghcr.io/grayslawson/osprey:v1.2.3
```

After reviewing the release notes and printed target, explicitly approve the
change with `--apply`. Only the `osprey` service is pulled and recreated; other
services in the Compose project are left alone:

```sh
scripts/osprey-update.sh \
  --file /var/lib/osprey/compose.release.yaml \
  --env-file /etc/osprey/release.env \
  --image ghcr.io/grayslawson/osprey:v1.2.3 --apply
```

Use `--latest` to resolve the newest stable GitHub release. It remains
check-only unless `--apply` is also supplied:

```sh
scripts/osprey-update.sh --env-file /etc/osprey/release.env --latest
```

For a rollback, run the same command with the previously known-good version or
digest and `--apply`. Keep one previous image until the new camera/Frigate
integration has been exercised. The helper does not perform an automatic
rollback: inspect camera and Frigate logs before making that decision.

### Optional scheduled checks or updates

The repository includes systemd units in [`contrib/systemd`](../contrib/systemd).
Copy the helper to `/usr/local/libexec/osprey-update.sh`, install the check unit
and timer, then enable only the timer:

```sh
sudo install -Dm755 scripts/osprey-update.sh /usr/local/libexec/osprey-update.sh
sudo install -Dm644 contrib/systemd/osprey-update-check.service /etc/systemd/system/
sudo install -Dm644 contrib/systemd/osprey-update-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now osprey-update-check.timer
```

The check writes the candidate release to the system journal and never changes
the running container. If unattended updates are appropriate for your host,
install `osprey-update-apply.service` and `osprey-update-apply.timer` instead,
then enable that timer only after reviewing its Docker-socket permissions and
maintenance window. This is deliberately opt-in; Osprey does not install a
socket-wide auto-updater or restart a deployment on its own.

## Development build

For contributors, `compose.yaml` builds from the checkout and mounts the source
trees. Run `./cuckoo/test.sh` before starting it. The development Compose file
is not a production upgrade path.
