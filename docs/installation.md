# Installation

Osprey runs as one service next to an existing Frigate. It does not install,
configure or run Frigate. Pick one install path:

| Path | Runs the controller | Notes |
|---|---|---|
| [Container image](#container-image) | Yes | The supported default. |
| [Home Assistant app](#home-assistant) | Yes | Host networking and ingress. |
| [NixOS module](nixos.md) | Yes | Flake package and systemd service. |
| [Proxmox](#proxmox) | Yes | Container in a guest, or a native LXC (pending). |
| [Python wheel](#python-wheel) | Yes | `osprey-ptz`, Python 3.13. |
| [Homebrew](#homebrew-chocolatey-and-npm) | No | `osprey doctor` launcher. |
| [Chocolatey](#homebrew-chocolatey-and-npm) | No | Installs the release Compose file. |
| [npm](#homebrew-chocolatey-and-npm) | No | Runs the read-only tools from the image. |

## Before you start

- Adopting a camera removes it from UniFi Protect. The camera has one
  controller at a time. You remove it from Protect and reset it, then the
  setup wizard points it at Osprey. [Uninstall and return to Protect](uninstall.md)
  walks back.
- A Linux host with a fixed LAN address that the camera and Frigate can
  both reach. The camera dials this address, so DHCP changes break it.
- Host networking for the container. Docker Desktop on macOS and Windows
  cannot serve a LAN camera.
- Camera and host on one network with the ports below open between them.
- A camera that is supported. See [camera models](camera-models.md).

Every path uses the same ports. The camera connects to Osprey on TCP 7442
(control, TLS), 7444 (snapshots) and 7550 (media). Clients use TCP 8000
(ONVIF and the console) and 8554 (RTSP). WS-Discovery uses UDP 3702, and the
setup wizard's camera scan uses UDP 10001.

## Container image

`ghcr.io/grayslawson/osprey` is published for `linux/amd64` and
`linux/arm64`. A final release is tagged `X.Y.Z`, `vX.Y.Z`, `X.Y` and
`latest`. A pre-release such as `0.9.0b3` gets only `0.9.0b3` and
`v0.9.0b3`. Each image is signed by digest with keyless cosign;
[`CONTRIBUTING.md`](../CONTRIBUTING.md#releasing) has the verify command.

### First run

`compose.release.yaml` is attached to every GitHub release. It uses host
networking and needs no settings: its image is the release it came from, and
Osprey works out the rest on first start. This needs 0.9.0b4 or later; for
0.9.0b3, use the [manual setup](#manual-setup).

```sh
mkdir osprey && cd osprey
curl -fsSLo compose.yaml https://github.com/grayslawson/osprey/releases/download/v0.9.0b3/compose.release.yaml
docker compose up -d --wait && docker compose logs osprey
```

On a new state volume Osprey opens the setup wizard by itself and logs a link
to it in a banner:

```text
  Osprey first-run setup. Open this link in a browser:

    http://192.0.2.10:8000/setup?token=...
```

The address in the link is the one on the network interface that holds the
host's default route. The token in it works for one save and expires after an
hour; after that the log shows a new link. The token lives only in memory, so a
restart also logs a new one.

The wizard goes through these steps:

1. Confirm the address the camera and Frigate use to reach Osprey, and choose
   an admin password for the console (at least 8 characters).
2. Pick the camera. Osprey scans the network for UniFi cameras and lists each
   one with its model and firmware, marked tested or untested. A camera that a
   controller still manages is marked too, with the steps to remove it from
   Protect and reset it, then scan again. If the camera is not listed, type its
   address and select **Look up**. Reserve the camera's address in your router:
   Osprey accepts video only from it.
3. Save. Osprey writes the configuration and restarts itself with it. The
   wizard shows the generated RTSP and ONVIF passwords once.
4. Adopt the camera. Osprey logs in to it (`ubnt`/`ubnt` on a reset camera, or
   a password you type) and points it at itself, then waits for the camera to
   connect and for video.
5. Add the camera to Frigate. The wizard shows a camera block for Frigate's
   config, with Osprey's address, ports and passwords filled in. Copy it, or
   give the Frigate URL and select **Add to Frigate**; see
   [add the camera to Frigate](#add-the-camera-to-frigate).
6. Read the check. It covers the camera's adoption, video, ONVIF at the
   advertised address with Frigate's login, and Frigate, if you gave its URL.
   Each failed check links to its fix in [troubleshooting](troubleshooting.md).

If you reload the page after the restart, sign in with the admin password and
the wizard picks up at the adopt step. You can open `/setup` again later, signed
in, to repeat any step.

The wizard writes these files to `/var/lib/osprey` in the state volume, all
mode `0600`:

| File | Contents |
|---|---|
| `osprey.json` | Runtime configuration. No secrets. |
| `osprey-secrets.env` | Admin password hash, user names and file paths. Loaded on start. |
| `rtsp-password`, `onvif-password` | Generated passwords. |
| `frigate.json` | The Frigate URL, once you give one. |

Every variable in the Compose file is optional. To change one, put it in a
`.env` file next to the Compose file, as in [`.env.example`](../.env.example).
`OSPREY_HOST` sets the advertised address; it then also beats the address you
confirm in the wizard. `OSPREY_IMAGE` runs another image, and `OSPREY_SETUP=0`
keeps the wizard closed.

### Host and bridge networking

With host networking (`compose.release.yaml`) the container shares the host's
network, so the detected address is the host's LAN address, the camera scan
reaches the LAN, and WS-Discovery works.

With a bridge, the container sits on a private network inside the host. The
camera cannot reach the container's own address, the scan's broadcast does not
leave the bridge, and WS-Discovery does not cross it. Use
`compose.release-bridge.yaml`, which publishes each port on `OSPREY_BIND`, the
host's LAN address. That variable is required there, and `OSPREY_HOST`
defaults to it:

```sh
OSPREY_BIND=192.0.2.10 docker compose -f compose.release-bridge.yaml up -d --wait
docker compose -f compose.release-bridge.yaml logs osprey
```

The link in the log names the container's own address, so open
`http://192.0.2.10:8000/setup?token=...` with the token from the link instead.
In the wizard, type the camera's address and select **Look up**: a direct
probe crosses the bridge where the scan cannot. Clients have to add Osprey by
address. Docker Desktop on macOS and Windows cannot serve a LAN camera in
either mode.

With `compose.release.yaml`, `OSPREY_BIND` is optional and only restricts
Osprey's listeners to one local address.

### Manual setup

Use this for 0.9.0b3, or to set the address and image yourself. Older Compose
files require `OSPREY_HOST` and `OSPREY_IMAGE`:

```sh
curl -fsSLO https://github.com/grayslawson/osprey/releases/download/v0.9.0b3/compose.release.yaml
export OSPREY_HOST=192.0.2.10   # this host's LAN address, as the camera sees it
export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:0.9.0b3
export OSPREY_SETUP=1           # open the setup wizard
docker compose -f compose.release.yaml up -d
docker compose -f compose.release.yaml logs osprey | grep 'setup token'
```

Open `http://<OSPREY_HOST>:8000/setup` and enter the token from the log. The
0.9.0b3 wizard asks for the camera's MAC address and does not adopt it:
restart Osprey after the save, then [point the camera at Osprey](#point-the-camera-at-osprey)
and [add it to Frigate](frigate-integration.md#camera-entry) by hand.

The wizard only opens while the configuration has no `host`, so leaving
`OSPREY_SETUP=1` set afterwards is harmless. If a save fails, setup stays
pending; restart and try again rather than copying temporary files.

### ONVIF authentication

Frigate's ONVIF user must be `frigate`, or a name in `ptz.autotrack_users`
(default `["frigate"]`). Moves by any other user count as a person taking
control. The wizard, and secure mode without the wizard, generate the ONVIF
user `frigate`. Without the wizard the password is in `onvif-password` in the
state volume:

```sh
docker compose exec osprey cat /var/lib/osprey/onvif-password
```

ONVIF credentials generated before 0.9.0b4 use the user `osprey`. Rename it
so autotracking works:

```sh
docker compose exec osprey sed -i \
  -e 's/^OSPREY_ONVIF_USERNAME=osprey$/OSPREY_ONVIF_USERNAME=frigate/' \
  /var/lib/osprey/osprey-secrets.env
docker compose restart osprey
```

That covers a wizard install. A secure install without the wizard keeps the
name in `/var/lib/osprey/onvif-username`; write `frigate` there and restart.
Then give Frigate `onvif.user: frigate`. You can also keep `osprey` and add it
to `ptz.autotrack_users`, or set your own with `OSPREY_ONVIF_USERNAME` and
`OSPREY_ONVIF_PASSWORD_FILE`.

### Rootless Podman

```sh
podman compose -f compose.release.yaml up -d
```

Rootless Podman may need explicit port permissions to reach a camera VLAN.

### Your own configuration file

To manage `osprey.json` yourself, for example for
[several cameras](multi-camera.md), [MQTT](mqtt.md) or the
[media persona](protect-dual-adoption.md), copy the wizard's file out of the
state volume and mount your copy over it in a Compose override:

```sh
docker compose -f compose.release.yaml cp osprey:/var/lib/osprey/osprey.json .
```

```yaml
# compose.override.yaml
services:
  osprey:
    volumes:
      - ./osprey.json:/var/lib/osprey/osprey.json:ro
```

Then run `docker compose -f compose.release.yaml -f compose.override.yaml up -d`.

To restrict the camera's address without the wizard, set it in the same
override: `OSPREY_EXTRA_ARGS: "--expected-camera-ip 192.0.2.20"` under
`environment`, or `ip` in a `cameras` entry.

All keys and environment variables are listed in
[configuration](configuration.md).

## Point the camera at Osprey

The camera has one native controller at a time. A camera that UniFi Protect
adopted keeps talking to Protect until it is released: remove it from Protect,
and reset it to factory settings if it still shows as managed, so it is back on the
default `ubnt`/`ubnt` login.

The setup wizard's adopt step does the rest. Without the wizard, run the same
step from inside the container:

```sh
docker compose exec osprey osprey adopt --camera 192.0.2.20
```

It asks for the camera's management password; on a reset camera, type `ubnt`.
`--user` defaults to `ubnt`, and Osprey's address and control port come from
its configuration (`--host` and `--control-port` override them). To skip the
prompt, set `OSPREY_CAMERA_PASSWORD` or pass `--password-file`; a `--password`
flag still works but shows up in `ps` and your shell history. The same command
is `osprey adopt` with the Python wheel and `./scripts/osprey adopt` (or
`./osprey/manage.sh`) in a source checkout.

The camera dials Osprey on port 7442 and Osprey adopts it; the console's
Health tab shows the adoption. To move a camera from a running Protect
controller without a reset, the maintainer script `osprey/handoff.sh` can do
it; see [the controller README](../osprey/README.md#testing-with-the-real-camera).
Afterwards, keep Protect from adopting it again with
[`osprey protect-guard`](protect-guard.md).

## Add the camera to Frigate

The wizard's Frigate step shows the camera block, and **Add to Frigate** puts
it in Frigate's config for you, through Frigate's own config API:

1. Osprey reads the config with `GET /api/config/raw`.
2. It adds the block as the first camera under `cameras:`, keeping the rest of
   the file, comments included, as it was. It never replaces a camera of the
   same name; choose another name instead.
3. It shows the change as a diff. Nothing is written yet.
4. When you confirm, it saves a copy of the current config to the state volume
   (`frigate-config-backup-<date>-<time>.yaml`, mode `0600`), then saves the new
   config with `POST /api/config/save`. Frigate checks the file and refuses an
   invalid one. Leave **Restart Frigate** ticked so it loads the camera.

If Frigate's config changed between the preview and the save, Osprey refuses
and asks you to preview again. A `cameras:` section written as `{}` or pulled
in with `!include` is left alone; add the block by hand there.

Frigate's unauthenticated port 5000 needs nothing else. On its authenticated
port 8971 the wizard asks for a Frigate admin user and password, and offers to
trust Frigate's own self-signed certificate. Osprey does not store them.

## Home Assistant

Osprey runs as a Home Assistant app (formerly add-on) with host networking
and ingress. Frigate can be another app or a separate host.

1. Open **Settings → Apps → Install app**, then the top-right menu →
   **Repositories**.
2. Add `https://github.com/grayslawson/osprey-ha-apps`.
3. Install **Osprey** and start it.
4. Open **Osprey** in the sidebar. The setup wizard opens there, with no link
   or token: Home Assistant has already signed you in. It detects the host's
   address, so `advertised_host` can stay empty; if you set it, it wins.

Options and Frigate settings are in the
[app documentation](../homeassistant/addon/osprey/DOCS.md). Osprey needs no
custom integration: Home Assistant's built-in ONVIF integration can use the
ONVIF endpoint directly. This is not a HACS integration.

## NixOS

The flake exports a package and a NixOS module that runs Osprey as a
`DynamicUser` systemd service. See [NixOS](nixos.md).

## Proxmox

The supported path today is a Debian or Ubuntu VM or unprivileged LXC running
Docker or Podman. Give it a bridged LAN interface that can reach the camera
and Frigate. Download
[`scripts/proxmox-install.sh`](../scripts/proxmox-install.sh) into the guest
and read its plan first:

```sh
curl -fsSLO https://raw.githubusercontent.com/grayslawson/osprey/v0.9.0b3/scripts/proxmox-install.sh
sh proxmox-install.sh --host 192.0.2.10 \
  --image ghcr.io/grayslawson/osprey:v0.9.0b3 --runtime podman
```

It is a dry run until you add `--apply`, which must run as root. Other options: `--bind-address`
(defaults to `--host`), `--state-dir` (default `/var/lib/osprey`),
`--onvif-port`, `--rtsp-port`, and `--force` to overwrite an existing Compose
file. The image must use the `v` tag form (`v0.9.0b3`) or a digest; the
script rejects `0.9.0b3`. It can install the runtime with `apt-get`, writes
`compose.yaml` under the state directory and starts only Osprey. It never
creates or changes a Proxmox VM or LXC. On first start the setup wizard opens;
the link is in `podman compose -f /var/lib/osprey/compose.yaml logs osprey`.
Continue as in [first run](#first-run).

A native LXC in the community-scripts style is in
[`packaging/proxmox`](../packaging/proxmox/README.md). It has not yet been run
on a Proxmox host.

## Python wheel

The `osprey-ptz` wheel bundles the controller and pyunifiwire behind one
`osprey` command. It needs Python 3.13 and `openssl` for the first-run
certificate. FFmpeg is optional and only used for the console's MJPEG
preview.

PyPI publishing starts with a later release. For 0.9.0b3, install the wheel
attached to the GitHub release:

```sh
python3.13 -m venv ~/.local/osprey && . ~/.local/osprey/bin/activate
pip install "osprey-ptz[mqtt] @ https://github.com/grayslawson/osprey/releases/download/v0.9.0b3/osprey_ptz-0.9.0b3-py3-none-any.whl"
osprey --config ~/.config/osprey/osprey.json --cert ~/.config/osprey/osprey.pem
```

Drop `[mqtt]` if you do not use [MQTT](mqtt.md). On first start Osprey opens
the wizard and logs its link, as in [first run](#first-run), and the wizard
writes its files next to `--config`. With 0.9.0b3, add `--setup --host` and
this host's address. Once Osprey has started with a host, a `--config` file
that does not exist is an error.

`osprey doctor`, `osprey probe` and `osprey protect-guard` ship in the same
command. Run the controller as an unprivileged user under systemd or launchd;
every port is above 1024. The wheel is a transitional launcher until the
package layout in `specs/002-python-package-cli` lands.

## Homebrew, Chocolatey and npm

These packages do not run the controller. Deploy it with the release Compose
file.

### Homebrew

The formula installs `osprey doctor` only, without `probe` or
`protect-guard`, and its doctor cannot validate a config file:

```sh
brew install grayslawson/osprey/osprey
```

### Chocolatey

The package is not on the Chocolatey community repository. It is a `.nupkg`
in the [`grayslawson/chocolatey-packages`](https://github.com/grayslawson/chocolatey-packages)
repository. Download it and install from that folder in an elevated
PowerShell:

```powershell
New-Item -ItemType Directory -Force C:\osprey-pkg | Out-Null
Invoke-WebRequest https://github.com/grayslawson/chocolatey-packages/raw/main/osprey.0.9.0-beta3.nupkg -OutFile C:\osprey-pkg\osprey.0.9.0-beta3.nupkg
choco install osprey --source C:\osprey-pkg --pre
```

It copies `compose.release.yaml` to `%ProgramData%\osprey`, adds an `osprey`
command that runs `docker compose` against that file, and starts nothing.
Create `%ProgramData%\osprey\.env` with `OSPREY_HOST` and `OSPREY_IMAGE`
(and `OSPREY_SETUP=1` for the wizard), then run `osprey up -d`. Docker
Desktop cannot serve a LAN camera in either networking mode, so the engine has
to run Linux containers with real host networking.

### npm

`@osprey-ptz/cli` runs `doctor`, `probe`, `protect-guard` and `version` from
the release image with Docker or Podman. npm publishing starts with a later
release, with pre-releases on the `next` dist-tag
(`npm install --global @osprey-ptz/cli@next`). For 0.9.0b3, install the
tarball attached to the GitHub release:

```sh
npm install --global https://github.com/grayslawson/osprey/releases/download/v0.9.0b3/osprey-ptz-cli-0.9.0b3.tgz
OSPREY_IMAGE=ghcr.io/grayslawson/osprey:0.9.0b3 osprey doctor --host 192.0.2.10
```

Each command runs in a fresh container that sees no host files, so
`probe --dump` and `protect-guard --api-key-file` cannot read files on the
host. Run `protect-guard` as
[Protect guard](protect-guard.md#with-osprey-protect-guard) shows instead.

## Verify a deployment

`osprey doctor` is read-only. It comes with the wheel, Homebrew and npm, and
as `./scripts/osprey doctor` in a source checkout. Give it the addresses that
apply:

```sh
osprey doctor \
  --host 192.0.2.10 \
  --camera 192.0.2.20 \
  --callback http://192.0.2.10:8000/healthz \
  --frigate http://frigate.example.com:5000
```

It prints one `OK`, `WARNING` or `ERROR` line per check and exits `1` if any
required check fails. It never changes the camera, firewall, Compose files,
Frigate or Osprey's configuration. A missing Compose binary is only a
warning. While Osprey runs, each of its own ports is reported as already
listening; run it before the first start to check that they are free.

To validate the configuration inside a running container:

```sh
docker compose -f compose.release.yaml exec osprey \
  /usr/local/bin/osprey-entrypoint doctor --host 192.0.2.10
```

The image has no `ss`, so the local port checks are skipped there; the
camera route, callback and Frigate checks work. npm runs
`doctor` in a fresh copy of the same image, without your config file. For
those checks, run the wheel, Homebrew or a checkout on the host.

## Updates and rollback

[Upgrading](upgrading.md) has the full routine, including a state backup and
the notes for older installs. Each `vX.Y.Z` tag publishes a GitHub release and a matching image. Watch the
repository with **Watch → Custom → Releases** to hear about them.

`scripts/osprey-update.sh` checks by default. It validates the Compose file
and prints the image it would use, without pulling or restarting anything:

```sh
scripts/osprey-update.sh \
  --file /var/lib/osprey/compose.release.yaml \
  --env-file /etc/osprey/release.env \
  --image ghcr.io/grayslawson/osprey:0.9.0b3
```

Add `--apply` to pull and recreate the `osprey` service. Other services in the
project are left alone. `--latest` resolves the newest stable release and is
also check-only without `--apply`. It skips pre-releases, so during the beta
use `--image`.

To roll back, run the same command with the previous tag or digest and
`--apply`. Keep the previous image until the new release has run with your
camera and Frigate. The helper never rolls back on its own.

### Scheduled checks

[`contrib/systemd`](../contrib/systemd) has a check timer that logs the
candidate release to the journal and changes nothing. The units run as an
`osprey` user in the `docker` group, read
`/var/lib/osprey/compose.release.yaml` and `/etc/osprey/release.env`, and use
`--latest`, so edit them to match your layout. Until a stable release exists
the check logs an error.

```sh
sudo install -Dm755 scripts/osprey-update.sh /usr/local/libexec/osprey-update.sh
sudo install -Dm644 contrib/systemd/osprey-update-check.service /etc/systemd/system/
sudo install -Dm644 contrib/systemd/osprey-update-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now osprey-update-check.timer
```

For unattended updates, install `osprey-update-apply.service` and
`osprey-update-apply.timer` instead. Review their Docker socket access and
maintenance window first.

## Development build

`compose.yaml` builds from the checkout and mounts the source trees. Run
`make test` first. It is not an upgrade path for a production camera.

It publishes only ONVIF and RTSP. To point a real camera at a development
build, add `-f compose.physical.yaml` with an env file copied from
`.env.physical.example`; add `-f compose.readonly.yaml` for the
[media-only ONVIF device](protect-dual-adoption.md).
