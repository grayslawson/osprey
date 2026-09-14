# Frigate connection settings

Osprey connects to an existing Frigate installation for health feedback and
convenient operator links. It does not install, start, configure, or modify
Frigate.

Authenticated operators can inspect or change the non-secret Frigate base URL:

```text
GET  /api/frigate
POST /api/frigate  {"base_url":"http://frigate.example:5000"}
```

The POST requires the normal operator session and CSRF token. Only `http` and
`https` URLs with a valid host and port are accepted. Credentials, query
strings, fragments, and other schemes are rejected. Set
`OSPREY_FRIGATE_CONFIG_FILE` to a root-owned JSON path if the value should
survive a restart; without it, the setting is memory-only.

This metadata is an integration hint. Osprey never uses it to mutate Frigate
configuration or grant access to the Frigate API. Keep Frigate's own
authentication and authorization in Frigate.
