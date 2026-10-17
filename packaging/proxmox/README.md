# Proxmox VE LXC (pending)

> **Status: pending.** These scripts need a published GitHub Release of
> Osprey. They have been shellchecked but not run on a Proxmox VE host.

`ct/osprey.sh` and `install/osprey-install.sh` follow the
[community-scripts](https://github.com/community-scripts/ProxmoxVE) layout:
the `ct/` script runs on the Proxmox VE host, creates an unprivileged
Debian 13 LXC, and runs the matching `install/` script inside it. Osprey is
installed natively (no Docker-in-LXC):

- the release source tarball is deployed to `/opt/osprey` with
  `fetch_and_deploy_gh_release`, and its version is recorded for
  `check_for_gh_release` updates;
- a Python venv in `/opt/osprey-venv` provides paho-mqtt;
- `/etc/osprey/osprey.env` sets `OSPREY_HOST` to the LXC's address and enables
  the first-run wizard; `/etc/osprey/credentials/admin-password` is passed to
  the service with systemd `LoadCredential`;
- state (certificate, wizard config, generated secrets) lives in
  `/var/lib/osprey`, and the hardened `osprey.service` runs as the `osprey`
  system user.

Running `ct/osprey.sh` again inside an existing container performs an update:
it backs up `/var/lib/osprey` and `/etc/osprey` to `/root`, redeploys the
latest release, and restarts the service.

## Plan

1. Publish GitHub Releases (see `.github/workflows/release.yml`).
2. After Phase 0 (`specs/002-python-package-cli`), install the `osprey-ptz`
   wheel with `uv tool install osprey-ptz==<version>` and run `osprey run`
   instead of the source tarball.
3. Test on a fresh Proxmox VE 9 host: create, open the UI, update in place.
4. Submit to `community-scripts/DevScripts`, which is where new apps start;
   pull requests straight to ProxmoxVE are closed. App metadata (logo, ports,
   default credentials) goes in their website record, not in this repository.

## Container-based alternative

To run the release image in an existing VM or LXC with Docker or Podman, use
[`scripts/proxmox-install.sh`](../../scripts/proxmox-install.sh). It runs as a
dry run unless you pass `--apply`.
