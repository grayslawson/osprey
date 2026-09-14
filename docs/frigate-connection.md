# Frigate connection settings

Osprey connects to an existing Frigate installation; it does not install,
start, configure, or bundle Frigate. Authenticated operators can inspect and
change the non-secret Frigate base URL through:

```text
GET  /api/frigate
POST /api/frigate   {"base_url":"http://frigate.example:5000"}
```

The POST requires the normal operator session and CSRF token. Only `http` and
`https` URLs are accepted. Credentials, query strings, fragments, invalid
ports, and other schemes are rejected. Set `OSPREY_FRIGATE_CONFIG_FILE` to a
root-owned JSON path for persistence; without it, the value is memory-only.
The setting is connection metadata and health/deep-link input only—Osprey never
uses it to mutate Frigate configuration.
