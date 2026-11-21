# Proxmox VE LXC (pending)

> **Status: pending.** These scripts are shellchecked but have not been run on
> a Proxmox VE host.

`ct/osprey.sh` and `install/osprey-install.sh` follow the
[community-scripts](https://github.com/community-scripts/ProxmoxVE) layout.
The `ct/` script runs on the Proxmox host, creates an unprivileged Debian 13
LXC and runs the `install/` script inside it. Osprey runs natively in the
container, without Docker:

- The release source archive is deployed to `/opt/osprey` with
  `fetch_and_deploy_gh_release`, and its version is recorded for
  `check_for_gh_release`.
- A virtualenv in `/opt/osprey-venv` provides paho-mqtt.
- `/etc/osprey/osprey.env` sets `OSPREY_HOST` to the container's address and
  turns on the setup wizard. `/etc/osprey/credentials/admin-password` reaches
  the service through systemd `LoadCredential`.
- State (certificate, wizard config, generated secrets) lives in
  `/var/lib/osprey`, and the hardened `osprey.service` runs as the `osprey`
  system user.

Running `ct/osprey.sh` again inside an existing container updates it: it
backs up `/var/lib/osprey` and `/etc/osprey` to `/root`, deploys the latest
release and restarts the service.

## Remaining work

1. Once `specs/002-python-package-cli` lands, install the wheel with
   `uv tool install osprey-ptz==<version>` instead of the source archive.
2. Test on a fresh Proxmox VE 9 host: create, open the console, update in
   place.
3. Submit to `community-scripts/DevScripts`, where new apps start. App
   metadata (logo, ports, default credentials) goes in their website record,
   not here.

## Container alternative

To run the release image in an existing VM or LXC with Docker or Podman, use
[`scripts/proxmox-install.sh`](../../scripts/proxmox-install.sh). It is a dry
run unless you pass `--apply`. See
[installation](../../docs/installation.md#proxmox).
