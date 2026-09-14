# Osprey operator authentication

The browser operator console can be protected with a password without putting
the password in a Compose file or process arguments. Osprey uses an in-memory
session cookie and a CSRF token for camera-control requests. ONVIF SOAP remains
available to Frigate and camera clients; the operator UI, preview, and control
API require the browser session.

Generate a password hash on the Osprey host (the password itself is never
printed by Osprey):

```sh
python3 -c 'import getpass,sys; sys.path.insert(0,"cuckoo"); from onvif import AdminAuth; print(AdminAuth.hash_password(getpass.getpass()))'
```

Put the resulting value in a root-only environment file outside the repository:

```dotenv
OSPREY_ADMIN_PASSWORD_HASH=pbkdf2_sha256$310000$...
```

Load that file when starting the controller. Alternatively set
`OSPREY_ADMIN_PASSWORD_FILE` to a root-only file containing one password; it is
hashed at startup and is not logged. A configured physical/release deployment
should always set one of these variables. If neither is present, authentication
is disabled for synthetic development compatibility and Osprey logs a warning.

Sessions are intentionally memory-only and are invalidated by a restart. Put a
TLS reverse proxy in front of a remotely reachable deployment; the built-in
HTTP server is intended for a trusted LAN.
