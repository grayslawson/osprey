# Osprey Assist

Osprey Assist is an optional helper that watches the camera's image and
suggests fixes. It is a separate process that talks to Osprey over its HTTP
API, so the controller itself stays small and does not need it.

This release adds the doors Assist needs. The helper itself comes next. The
doors are useful on their own.

## What is in the controller

- **A day or night signal.** `scene` is in the state document, in the `ptz`
  event and in the MQTT `state/ptz` message.
- **A lowest priority for Assist.** Its moves rank with the patrol's. A person
  holds it off, Frigate takes the camera from it, and it never starts a manual
  hold. A move that a person's hold dropped is refused, not reported as done.
- **A token for Assist.** It is a bearer token, not a console login. It can call
  a short list of routes, and for ISP it can change only a short list of
  settings. See the [API](api.md#osprey-assist-routes).
- **A patrol hold and dwell override.** Assist can pause the patrol for a few
  minutes, or lengthen a stop for a while. Both expire on their own and neither
  is saved. See [Sentry](sentry.md#pausing-and-reshaping-the-patrol-at-runtime).
- **A console door.** `/api/v1/assist/...` forwards the console's requests to
  Assist once you are signed in.
- **A night view menu** on each patrol position in the console.

## Switching the door on

Nothing is open by default. Either set `assist.enabled` in `osprey.json`, or set
`OSPREY_ASSIST_TOKEN_FILE`:

```json
{"assist": {"enabled": true, "url": "http://127.0.0.1:8090", "scopes": ["quality", "presets"]}}
```

Osprey creates the token file on first start, readable only by its user, and
never changes it after that. Delete the file and restart to rotate the token.
Remove `assist.enabled` (and the environment variable) to close the door.

`scopes` limits what the token can do. `quality` is reading, moving, going to
presets, ISP and the patrol hold. `presets` adds saving presets and setting a
patrol point's night view. Both are on by default.

The token is a password for the camera's image settings and head position. Keep
it out of backups you share, and do not publish Assist's port.
