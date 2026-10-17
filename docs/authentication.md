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
deployment.

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

The Home Assistant add-on reaches Osprey through ingress. Its requests carry an
`X-Ingress-Path` header, and Osprey builds every redirect, cookie path and
page URL under that prefix. Ingress requests still need the password unless
`OSPREY_TRUSTED_INGRESS_PEER` names the supervisor's fixed address. Only
requests from exactly that peer that carry a valid ingress path are then
treated as signed in, and they use a per-process CSRF token. Leave this unset
unless the add-on's network setup requires it.

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

After signing in, the **Logs** view shows a bounded, recent tail of Osprey
application records, updated live. The `/legacy` console has a **Logs & alerts**
panel that polls every five seconds. Both can be filtered to warnings or errors. The alert badge counts warnings and
errors retained in the in-memory window; it is not a replacement for the
host journal or container logs, which remain the source for complete history.

Log messages are capped and common credential-like values are redacted before
they reach the browser. Keep the console authenticated and use the host's
normal log retention and access controls for incident investigation.
