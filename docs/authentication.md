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

Alternatively, set `OSPREY_ADMIN_PASSWORD_FILE` to a root-readable file that
contains one password. Configure exactly one of these methods in a release
deployment. If neither is set, authentication is disabled for local
development and a warning is logged.

ONVIF SOAP remains available to Frigate and other camera clients; it does not
inherit the browser session. Configure ONVIF WS-Security separately as
described in [security](security.md). Put a TLS reverse proxy in front of the
console when it is reachable from an untrusted network.

The browser password protects only the operator console and its JSON control
endpoints. It does not authenticate ONVIF or RTSP. To protect ONVIF PTZ and
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
