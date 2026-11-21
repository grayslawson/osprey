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

Every path uses the same ports. The camera connects to Osprey on TCP 7442
(control, TLS), 7444 (snapshots) and 7550 (media). Clients use TCP 8000
(ONVIF and the console) and 8554 (RTSP). WS-Discovery uses UDP 3702.

## Container image

`ghcr.io/grayslawson/osprey` is published for `linux/amd64` and
`linux/arm64`. A final release is tagged `X.Y.Z`, `vX.Y.Z`, `X.Y` and
`latest`. A pre-release such as `0.9.0b2` gets only `0.9.0b2` and
`v0.9.0b2`. Each image is signed by digest with keyless cosign;
[`CONTRIBUTING.md`](../CONTRIBUTING.md#releasing) has the verify command.

### First run

`compose.release.yaml` is attached to every GitHub release and uses host
networking. `OSPREY_HOST` and `OSPREY_IMAGE` are required:

```sh
curl -fsSLO https://github.com/grayslawson/osprey/releases/download/v0.9.0b2/compose.release.yaml
export OSPREY_HOST=192.0.2.10   # this host's LAN address, as the camera sees it
export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:0.9.0b2
export OSPREY_SETUP=1           # open the setup wizard
docker compose -f compose.release.yaml up -d
docker compose -f compose.release.yaml logs osprey | grep 'setup token'
```

Every later `docker compose` command needs the same variables. To avoid
exporting them in each shell, put them in a `.env` file next to
`compose.release.yaml`, as in [`.env.example`](../.env.example).

Open `http://<OSPREY_HOST>:8000/setup` and enter the token from the log. The
wizard asks for the advertised host, bind address, camera MAC and name,
optional camera IP, Frigate URL and an admin password of at least 8
characters. It writes these files to `/var/lib/osprey` in the state volume,
all mode `0600`:

| File | Contents |
|---|---|
| `osprey.json` | Runtime configuration. No secrets. |
| `osprey-secrets.env` | Admin password hash, RTSP user and file paths. Loaded on start. |
| `rtsp-password`, `onvif-password` | Generated passwords. |
| `frigate.json` | The Frigate URL. |

The generated RTSP and ONVIF passwords are shown once. RTSP Digest
authentication is on after the restart. ONVIF stays anonymous; the lines that
enable it are commented out in `osprey-secrets.env` until you have tested
Frigate with them.

Restart to load the new configuration:

```sh
docker compose -f compose.release.yaml restart osprey
```

The wizard only opens while the configuration has no `host`, so leaving
`OSPREY_SETUP=1` set afterwards is harmless. The setup token lives only in
memory. If you lose it, restart and read the new one from the log. If a save
fails, setup stays pending; restart and try again rather than copying
temporary files.

### ONVIF authentication

When you turn ONVIF authentication on, name the user `frigate`. Only users in
`ptz.autotrack_users` (default `["frigate"]`) count as autotrackers, so with
the wizard's `osprey` user every Frigate move would count as a person taking
control. This uncomments both lines and renames the user:

```sh
docker compose -f compose.release.yaml exec osprey sed -i \
  -e 's/^# OSPREY_ONVIF_USERNAME=osprey$/OSPREY_ONVIF_USERNAME=frigate/' \
  -e 's/^# OSPREY_ONVIF_PASSWORD_FILE=/OSPREY_ONVIF_PASSWORD_FILE=/' \
  /var/lib/osprey/osprey-secrets.env
docker compose -f compose.release.yaml restart osprey
```

Then give Frigate `onvif.user: frigate` and the ONVIF password the wizard
showed.

### Rootless Podman

```sh
podman compose -f compose.release.yaml up -d
```

Rootless Podman may need explicit port permissions to reach a camera VLAN.

### Bridge networking

Use `compose.release-bridge.yaml` when host networking is not available. It
publishes each port on `OSPREY_BIND`, which is then required and must be the
host's LAN address. WS-Discovery multicast does not cross a bridge, so
clients have to add Osprey by address. Docker Desktop on macOS and Windows
cannot serve a LAN camera in either mode.

With `compose.release.yaml`, `OSPREY_BIND` is optional and only restricts
Osprey's listeners to one local address.

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

All keys and environment variables are listed in
[configuration](configuration.md).

## Point the camera at Osprey

The camera has one native controller at a time. A camera already adopted by
UniFi Protect keeps talking to Protect until it is released: remove it from
Protect and factory-reset it, so it is back on the default `ubnt`/`ubnt`
login. Then point it at Osprey from a source checkout on any machine that can
reach the camera over HTTPS (Python 3 is enough):

```sh
git clone https://github.com/grayslawson/osprey.git && cd osprey
./osprey/manage.sh --camera 192.0.2.20 --user ubnt --password ubnt \
  --host 192.0.2.10
```

`--host` is Osprey's address (`OSPREY_HOST`, or `advertised_host` in Home
Assistant). The camera dials it on port 7442 and Osprey adopts it; the
console's Health tab shows the adoption. To move a camera from a running
Protect controller without a reset, see `osprey/handoff.sh` and
[the controller README](../osprey/README.md#testing-with-the-real-camera).
Afterwards, keep Protect from re-adopting it with
[`osprey protect-guard`](protect-guard.md).

## Home Assistant

Osprey runs as a Home Assistant app (formerly add-on) with host networking
and ingress. Frigate can be another app or a separate host.

1. Open **Settings → Apps → Install app**, then the top-right menu →
   **Repositories**.
2. Add `https://github.com/grayslawson/osprey-ha-apps`.
3. Install **Osprey**, set `advertised_host`, and start it.

Options and Frigate settings are in the
[app documentation](../homeassistant/addon/osprey/DOCS.md). Osprey needs no
custom integration: Home Assistant's built-in ONVIF integration can use the
ONVIF endpoint directly. The Marketplace (formerly HACS) does not list apps,
so it plays no part here.

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
curl -fsSLO https://raw.githubusercontent.com/grayslawson/osprey/v0.9.0b2/scripts/proxmox-install.sh
sh proxmox-install.sh --host 192.0.2.10 \
  --image ghcr.io/grayslawson/osprey:v0.9.0b2 --runtime podman
```

It is a dry run until you add `--apply`, which must run as root. Other options: `--bind-address`
(defaults to `--host`), `--state-dir` (default `/var/lib/osprey`),
`--onvif-port`, `--rtsp-port`, and `--force` to overwrite an existing Compose
file. The image must use the `v` tag form (`v0.9.0b2`) or a digest; the
script rejects `0.9.0b2`. It can install the runtime with `apt-get`, writes
`compose.yaml` under the state directory and starts only Osprey. It never
creates or changes a Proxmox VM or LXC. The setup wizard is off in that file:
add `OSPREY_SETUP: "1"` to its `environment`, run
`podman compose -f /var/lib/osprey/compose.yaml up -d` again, and continue as
in [first run](#first-run).

A native LXC in the community-scripts style is in
[`packaging/proxmox`](../packaging/proxmox/README.md). It has not yet been run
on a Proxmox host.

## Python wheel

The `osprey-ptz` wheel bundles the controller and pyunifiwire behind one
`osprey` command. It needs Python 3.13 and `openssl` for the first-run
certificate. FFmpeg is optional and only used for the console's MJPEG
preview.

PyPI publishing starts with a later release. For 0.9.0b2, install the wheel
attached to the GitHub release:

```sh
python3.13 -m venv ~/.local/osprey && . ~/.local/osprey/bin/activate
pip install "osprey-ptz[mqtt] @ https://github.com/grayslawson/osprey/releases/download/v0.9.0b2/osprey_ptz-0.9.0b2-py3-none-any.whl"
osprey --setup --host 192.0.2.10 --config ~/.config/osprey/osprey.json \
  --cert ~/.config/osprey/osprey.pem
```

Drop `[mqtt]` if you do not use [MQTT](mqtt.md). `--setup` opens the wizard
and logs its token, as in [first run](#first-run), and the wizard writes its
files next to `--config`. Without `--setup`, a `--config` file that does not
exist is an error.

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
Invoke-WebRequest https://github.com/grayslawson/chocolatey-packages/raw/main/osprey.0.9.0-beta2.nupkg -OutFile C:\osprey-pkg\osprey.0.9.0-beta2.nupkg
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
(`npm install --global @osprey-ptz/cli@next`). For 0.9.0b2, install the
tarball attached to the GitHub release:

```sh
npm install --global https://github.com/grayslawson/osprey/releases/download/v0.9.0b2/osprey-ptz-cli-0.9.0b2.tgz
OSPREY_IMAGE=ghcr.io/grayslawson/osprey:0.9.0b2 osprey doctor --host 192.0.2.10
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

The image has no `ip`, `ss` or `curl`, so there `--camera` and `--callback`
report errors and the port and `--frigate` checks are skipped. npm runs
`doctor` in a fresh copy of the same image, without your config file. For
those checks, run the wheel, Homebrew or a checkout on the host.

## Updates and rollback

Each `vX.Y.Z` tag publishes a GitHub release and a matching image. Watch the
repository with **Watch → Custom → Releases** to hear about them.

`scripts/osprey-update.sh` checks by default. It validates the Compose file
and prints the image it would use, without pulling or restarting anything:

```sh
scripts/osprey-update.sh \
  --file /var/lib/osprey/compose.release.yaml \
  --env-file /etc/osprey/release.env \
  --image ghcr.io/grayslawson/osprey:0.9.0b2
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
