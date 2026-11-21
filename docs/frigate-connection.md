# Frigate connection settings

Osprey stores a Frigate base URL for the console's Health check and links. It
never installs, starts, configures or calls Frigate's API to change anything.

Signed-in operators can read or change it:

```text
GET  /api/frigate
POST /api/frigate  {"base_url":"http://frigate.example.com:5000"}
```

The POST needs the session and CSRF token, like every other change. Only
`http` and `https` URLs with a valid host and port are accepted; credentials,
query strings, fragments and other schemes are rejected.

The value is kept in the file named by `OSPREY_FRIGATE_CONFIG_FILE`. The setup
wizard and the NixOS module set it to `frigate.json` in the state directory.
Without it, the setting lasts until the next restart.

Frigate's own authentication stays in Frigate.
