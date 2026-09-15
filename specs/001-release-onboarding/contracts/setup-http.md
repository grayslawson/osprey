# Setup HTTP Contract

The first-run endpoint is available only while setup is pending. Exact route names follow the
running Osprey server; this contract describes the stable behavior rather than a framework.

## Status

`GET /setup/status`

Response fields:

```json
{
  "pending": true,
  "config_path": "...",
  "secrets_path": "...",
  "restart_required": false
}
```

Paths are informational and MUST NOT contain secret contents.

## Save

`POST /setup`

Request body:

```json
{
  "setup_token": "one-time startup token",
  "host": "192.0.2.10",
  "bind": "0.0.0.0",
  "name": "front-door",
  "mac": "001122AABBCC",
  "ip": "192.0.2.20",
  "frigate_url": "http://frigate.example.invalid:5000",
  "admin_password": "operator supplied password"
}
```

Valid requests return `200` with `ok`, paths, restart state, and generated credentials only
where the setup UI needs to display them once. Implementations MUST NOT log the response.

Invalid input, an invalid token, or a completed setup returns a client-visible error without
writing partial state. Replaying a successful request after completion MUST NOT overwrite it.
