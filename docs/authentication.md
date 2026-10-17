# Operator authentication

The browser operator console can be protected with a password without placing
the plaintext password in Compose arguments. Osprey stores only in-memory
sessions; each session carries a CSRF token for camera-control requests.

Generate a PBKDF2 password hash on the Osprey host. The password itself is not
printed by Osprey:

```sh
python3 -c 'import getpass,sys; sys.path.insert(0,"cuckoo"); from onvif import AdminAuth; print(AdminAuth.hash_password(getpass.getpass()))'
```

Put the resulting value in a root-only environment file outside the repository:

```dotenv
OSPREY_ADMIN_PASSWORD_HASH=pbkdf2_sha256$310000$...
```

The first-run wizard creates this file as `/state/osprey-secrets.env` and Osprey
loads it automatically on the next restart. Set `OSPREY_SECRETS_FILE` when the
file lives elsewhere. Generated files must remain mode `0600`.

Alternatively, set `OSPREY_ADMIN_PASSWORD_FILE` to a root-readable file that
contains one password. Configure exactly one of these methods in a release
deployment. If neither is set, authentication is disabled for local
development and a warning is logged.

ONVIF SOAP remains available to Frigate and other camera clients; it does not
inherit the browser session. Configure ONVIF WS-Security separately as
described in [security](security.md). Put a TLS reverse proxy in front of the
console when it is reachable from an untrusted network.

The browser password protects only the operator console and its JSON control
endpoints; it does not authenticate ONVIF or RTSP. Configure RTSP Digest
credentials separately with `OSPREY_RTSP_USERNAME` and
`OSPREY_RTSP_PASSWORD_FILE`. To protect ONVIF PTZ and
metadata requests, configure the separate WS-Security credentials described in
[the security guide](security.md); configure those same credentials in
Frigate's camera `onvif.user` and `onvif.password` fields.

## Operator logs

After signing in, the console's **Logs & alerts** panel shows a bounded,
recent tail of Osprey application records. It polls every five seconds and
can be filtered to warnings or errors. The alert badge counts warnings and
errors retained in the in-memory window; it is not a replacement for the
host journal or container logs, which remain the source for complete history.

Log messages are capped and common credential-like values are redacted before
they reach the browser. Keep the console authenticated and use the host's
normal log retention and access controls for incident investigation.

## Multiple ONVIF principals

Set `OSPREY_ONVIF_USERS_FILE` to a JSON file to give each client its own
credential and roles. Without it, the single-credential settings
(`OSPREY_ONVIF_USERNAME` with `OSPREY_ONVIF_PASSWORD` or
`OSPREY_ONVIF_PASSWORD_FILE`) keep working, and that user holds every role.
With neither set, ONVIF is anonymous, as before.

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
