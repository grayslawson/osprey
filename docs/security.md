# Security

Osprey's ONVIF and RTSP services speak to ordinary camera clients on the local
network. Treat them as camera-network services, not Internet services.

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
| 8000, ONVIF | Anonymous: any reachable host can read streams and events and move the camera | WS-Security UsernameToken, per-user roles |
| 8000, `/healthz` and `/metrics` | Always open, read-only | Same |
| 8001 (optional), media persona | Media and events, no PTZ | Same as ONVIF |
| 8554, RTSP | Anonymous | Digest; video is still unencrypted |
| 7442, 7444, 7550 | Camera only; restrict them to the camera's address | Same |

ONVIF access never grants the console session, MQTT credentials or the
camera's control channel, but it can move the camera, so treat ONVIF network
access as control access. `/healthz` and `/metrics` return state and counters
only, never a MAC, address, preset name or credential.

## ONVIF authentication

ONVIF authentication is opt-in, because some Frigate setups start anonymous.
Set `OSPREY_ONVIF_USERNAME` with `OSPREY_ONVIF_PASSWORD_FILE` (or
`OSPREY_ONVIF_PASSWORD`) to require a `PasswordDigest` UsernameToken on every
SOAP call, with no anonymous fallback. For one user per client, use
`OSPREY_ONVIF_USERS_FILE`; see
[ONVIF principals](authentication.md#multiple-onvif-principals).

Each nonce is accepted once, and timestamps more than 300 seconds from
Osprey's clock are rejected. Change the window with
`OSPREY_ONVIF_MAX_SKEW_SECONDS` (30 to 3600).

In Frigate, set the camera's `onvif.user` and `onvif.password`, and leave
`onvif.tls_insecure` at `false`: Frigate treats `true` as "skip the digest",
and Osprey then rejects the request. Test discovery, presets, movement,
events and autotracking before you enable authentication on a running
installation. Clients without WS-Security support will stop working.

## RTSP authentication

Set `OSPREY_RTSP_USERNAME` with `OSPREY_RTSP_PASSWORD_FILE` (or
`OSPREY_RTSP_PASSWORD`) to require Digest authentication with expiring
nonces. The setup wizard turns this on. RTSP authentication is independent of
ONVIF authentication, and it does not encrypt video: use network ACLs or a
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
