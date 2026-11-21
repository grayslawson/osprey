# Authentication

This page covers the console password, sessions, Home Assistant ingress and
ONVIF users. Port exposure and RTSP are in [security](security.md).

## Console password

The setup wizard sets the password for you. To set it by hand, generate a
PBKDF2 hash from a source checkout. The password is never printed:

```sh
python3 -c 'import getpass,sys; sys.path.insert(0,"osprey"); from onvif import AdminAuth; print(AdminAuth.hash_password(getpass.getpass()))'
```

Put the hash in `osprey-secrets.env`, a root-only file outside the
repository:

```dotenv
OSPREY_ADMIN_PASSWORD_HASH=pbkdf2_sha256$310000$...
```

Osprey loads that file from the config file's directory on start. In the
container it is `/var/lib/osprey/osprey-secrets.env`, which the wizard
writes. Set `OSPREY_SECRETS_FILE` if the file lives elsewhere, and keep it mode
`0600`.

Or set `OSPREY_ADMIN_PASSWORD_FILE` to a file that holds the password. Use one
method, not both.

Without a password the console is **locked**: the browser app and `/legacy`
answer `503` with a hint, and no camera control is possible. The public
snapshot and the ONVIF and RTSP services are not affected. For local development
only, set `OSPREY_ADMIN_AUTH=disabled` to open the console without a password;
Osprey logs a warning on every start while this is set.

## Sessions, CSRF and throttling

- Sign-in sets an `osprey_session` cookie that is `HttpOnly`, `SameSite=Strict`,
  and scoped to the app's path. It is marked `Secure` when the browser reached
  Osprey over HTTPS, which Osprey detects from `X-Forwarded-Proto` or
  `Forwarded`. Set `OSPREY_COOKIE_SECURE=1` when a proxy sends neither header.
- Every mutating request needs the `X-CSRF-Token` header that the session
  returns. The cookie alone is never accepted as proof.
- `DELETE /api/v1/session` (the **Sign out** button) ends the session.
- A wrong password returns `401`. Each client address may fail five times;
  after that it is locked out for an exponentially growing interval, up to
  five minutes, and gets `429` with `Retry-After`.

## Home Assistant ingress

Home Assistant's ingress proxy sends an `X-Ingress-Path` header. Osprey
accepts only the `/api/hassio_ingress/<token>` form and builds every redirect,
cookie path and page URL under it.

When `OSPREY_TRUSTED_INGRESS_PEER` names the proxy's address, requests from
exactly that peer with a valid ingress path count as signed in, because Home
Assistant has already authenticated the user. They use a per-process CSRF
token. The Home Assistant app sets this to the Supervisor's ingress proxy, so
the console opened from the sidebar needs no second password. Direct access
to port 8000 still needs the admin password. Leave the variable unset outside
Home Assistant.

The console password covers the console and its JSON API only. It does not
authenticate ONVIF or RTSP; see [security](security.md) for those.

## Operator logs

The **Logs** tab shows a bounded, live tail of recent log records, and the
`/legacy` console has a **Logs & alerts** panel that polls every five seconds.
Both filter to warnings or errors, and the alert badge counts those in the
in-memory window. Messages are capped and credential-like values are redacted
before they reach the browser.

The full log is in the host journal or container log, and in
`logs/osprey.log` next to the control certificate (5 MiB, with three rotated
copies).

## Multiple ONVIF principals

Set `OSPREY_ONVIF_USERS_FILE` to a JSON file to give each client its own
credential and roles. Without it, the single-credential settings
(`OSPREY_ONVIF_USERNAME` with `OSPREY_ONVIF_PASSWORD` or
`OSPREY_ONVIF_PASSWORD_FILE`) keep working, and that user holds every role.
With neither set, ONVIF is anonymous.

```json
{
  "users": [
    {"username": "frigate", "password_file": "/run/secrets/osprey-frigate",
     "roles": ["media", "events", "ptz"]},
    {"username": "protect", "password_file": "/run/secrets/osprey-protect",
     "roles": ["media", "events"], "allow_from": ["192.0.2.30/32"]},
    {"username": "admin", "password_file": "/run/secrets/osprey-admin",
     "roles": ["admin"]}
  ],
  "rtsp": {"require_auth": true, "anonymous_from": []}
}
```

Roles: `media` (metadata, stream and snapshot URIs, snapshots), `events`
(pull-point subscriptions), `ptz` (status, movement, Stop, presets) and `admin`
(everything, including encoder writes). Any authenticated client may read basic
device information. `allow_from` limits a user to the listed addresses or CIDRs.
Each password comes from exactly one of `password` or `password_file`. Use
`password_file` for real deployments so the secret stays out of the JSON.

The `rtsp` section applies the same users to RTSP Digest. Set
`anonymous_from` only for a client that is proven not to send RTSP credentials.
Pre-auth ONVIF calls (`GetSystemDateAndTime`, `GetCapabilities`, `GetServices`,
`GetWsdlUrl`, `GetServiceCapabilities`) need no credentials, because clients
call them first to correct their clock.

WS-Security replay protection is on for every user by default. A client that
reuses nonces can be given `"replay_protection": false`. Do this only after a
packet capture confirms the reuse, because it weakens protection against
replay within the clock-skew window.
