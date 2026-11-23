# Security

Osprey's ONVIF and RTSP services speak to ordinary camera clients on the local
network. Treat them as camera-network services, not Internet services.

## Secure mode and legacy mode

Osprey picks one of two modes the first time it starts and writes the choice
to `security-mode` in the state directory. It never changes it on its own, so
an upgrade does not change what a running install does.

| | Secure | Legacy |
|---|---|---|
| Picked when | The state directory has no certificate or config yet: a new install | A certificate or config already exists: an install from before this was added |
| ONVIF | Requires credentials | Anonymous unless you configured users |
| RTSP | Requires Digest credentials | Anonymous unless you configured a user |
| Media and snapshot ports | Accept only the adopted camera's address | Accept any host, unless you set the camera's address |

In secure mode, Osprey generates a password for each service that has none
configured and writes it to `<state>/onvif-password` and
`<state>/rtsp-password`, mode `0600`. Both belong to the user `osprey`. The
passwords are also logged once, on the first start, so save them from the log
then. The setup wizard writes the same two files and shows the passwords when
it finishes.

A legacy install warns at every start that ONVIF or RTSP is anonymous, and
logs once, when the mode is first recorded, how to tighten it. To switch an existing install, see [upgrading](upgrading.md#move-a-legacy-install-to-secure-mode).

| Variable | Meaning |
|---|---|
| `OSPREY_SECURITY` | `secure` or `legacy`. Overrides the saved mode. |
| `OSPREY_ONVIF_AUTH` | `disabled` keeps ONVIF anonymous in secure mode. |
| `OSPREY_RTSP_AUTH` | `disabled` keeps RTSP anonymous in secure mode. |

Opt out per service only on a network you trust. In the Home Assistant app the
same choices are the `security`, `onvif_*` and `rtsp_*` options.

### The camera's address

The media (7550) and snapshot (7444) ports have no login. They are camera only
in secure mode, by address:

- If you set the camera's address, with `cameras[].ip` or `--expected-camera-ip`
  (`camera_ip` in the Home Assistant app), only that address is accepted.
- If you did not, Osprey learns it. The first camera that completes the control
  handshake adds its address, and nothing else is accepted before that.

A configured address is the stronger choice, because the control port can be
reached by any host on the network until the camera has connected. Reserve the
camera's address in your router and set it. Learned addresses live in memory, so
a restart learns them again.

## Network

- Allow only the camera, Frigate and approved NVRs to reach Osprey's ports,
  with a host firewall or a dedicated VLAN. Never forward them to the
  Internet.
- `OSPREY_BIND` restricts the listeners to one address. Do not listen on
  `0.0.0.0` on an untrusted host.
- Put the console behind an HTTPS reverse proxy when you reach it from
  another network.

## What each port exposes

| Port | Without credentials | With credentials configured |
|---|---|---|
| 8000, console and `/api` | Locked (`503`) until an admin password is set | Password sign-in, CSRF on every change |
| 8000, ONVIF | Anonymous in legacy mode: any reachable host can read streams and events and move the camera | WS-Security UsernameToken, per-user roles. Required in secure mode. |
| 8000, `/healthz` and `/metrics` | Always open, read-only | Same |
| 8001 (optional), media persona | Media and events, no PTZ | Same as ONVIF |
| 8554, RTSP | Anonymous in legacy mode | Digest; video is still unencrypted. Required in secure mode. |
| 7442 | The camera, over TLS. Any host can open it, so restrict it to the camera's address | Same |
| 7444, 7550 | Camera only in secure mode. Any host in legacy mode unless you set the camera's address | Same |

ONVIF access never grants the console session, MQTT credentials or the
camera's control channel, but it can move the camera, so treat ONVIF network
access as control access. `/healthz` and `/metrics` return state and counters
only, never a MAC, address, preset name or credential.

## ONVIF authentication

In secure mode ONVIF authentication is on, with the generated user `osprey`. In
legacy mode it is opt-in. To choose your own user, set `OSPREY_ONVIF_USERNAME`
with `OSPREY_ONVIF_PASSWORD_FILE` (or `OSPREY_ONVIF_PASSWORD`). Osprey then
requires a `PasswordDigest` UsernameToken on every SOAP call, with no
anonymous fallback. For one user per client, use
`OSPREY_ONVIF_USERS_FILE`; see
[ONVIF principals](authentication.md#multiple-onvif-principals).

Each nonce is accepted once, and timestamps more than 300 seconds from
Osprey's clock are rejected. Change the window with
`OSPREY_ONVIF_MAX_SKEW_SECONDS` (30 to 3600).

Frigate has to use the user name `frigate`, or a name listed in
`ptz.autotrack_users`. Any other ONVIF user counts as a person, so every move
Frigate makes would hold Frigate off for `ptz.manual_hold_sec`. The generated
user is `osprey`, so either rename it (see
[installation](installation.md#onvif-authentication)) or add `osprey` to
`ptz.autotrack_users`.

In Frigate, set the camera's `onvif.user` and `onvif.password`, and leave
`onvif.tls_insecure` at `false`: Frigate treats `true` as "skip the digest",
and Osprey then rejects the request. Test discovery, presets, movement,
events and autotracking before you enable authentication on a running
installation. Clients without WS-Security support will stop working.

## RTSP authentication

Set `OSPREY_RTSP_USERNAME` with `OSPREY_RTSP_PASSWORD_FILE` (or
`OSPREY_RTSP_PASSWORD`) to require Digest authentication with expiring
nonces. Secure mode and the setup wizard turn this on. RTSP authentication is
independent of ONVIF authentication, and it does not encrypt video: use network ACLs or a
TLS tunnel across untrusted networks.

## Console

Without a password the console is locked, not open. Sign-in is throttled,
every change needs the `X-CSRF-Token` header, and every response carries a
strict Content-Security-Policy, `X-Frame-Options` and `nosniff`. See
[authentication](authentication.md).

## MQTT

MQTT is off by default. When you enable it, use TLS, broker authentication and
ACLs that let only trusted clients publish to Osprey's command topics. Osprey
accepts only its own command topics and ignores retained commands.

## Input limits

SOAP bodies are capped at 256 KiB before XML parsing. The Frigate URL must be
`http` or `https` with no credentials, query or fragment. PTZ, zoom, preset,
patrol and MQTT inputs are validated and bounded.

## Secrets

| Secret | Where Osprey reads it | Notes |
|---|---|---|
| Console password | `OSPREY_ADMIN_PASSWORD_FILE` or `OSPREY_ADMIN_PASSWORD_HASH` | The wizard writes the hash to `osprey-secrets.env`, mode `0600`. |
| ONVIF users | `OSPREY_ONVIF_USERS_FILE`, with a `password_file` per user | Never logged. |
| RTSP password | `OSPREY_RTSP_PASSWORD_FILE` | Protects the password, not the video. |
| MQTT password | `mqtt.password` in `osprey.json` | The one secret in the config file. Keep it `0600` and out of Git. |
| Control certificate | `--cert` (default `osprey.pem`, mode `0600`) | Generated on first run. The camera trusts it, so back it up. |
| UniFi Network API key | `--api-key-file` or `OSPREY_UNIFI_API_KEY_FILE` | Only `protect-guard` and `doctor` use it. Delete it after use. |

- Prefer the `_FILE` variables. Plain values show up in `docker inspect`, `ps`
  and crash reports.
- `.gitignore` covers `osprey.json`, `osprey.pem` and the old `cuckoo.*`
  names. Never commit them.
- Logs never contain credential values. `onvif.log_calls` logs user names and
  peer addresses, so treat those logs as internal.
- `osprey probe` bundles are redacted. Read one before you attach it to an
  issue, and never attach a raw `--dump` file.

To report a vulnerability, follow [`SECURITY.md`](../SECURITY.md).
