# Operator HTTP contract (`/api/v1`)

Owner: `cuckoo/webapi.py`. Every route is served on the ONVIF port and requires
the operator session except `GET /`, `/ui/*` and `/api/v1/session`. Camera-scoped routes take
`{cam}`, which is `default` for a single-camera deployment or a registered
camera ID.

## Conventions

- Bodies are JSON, at most 4 KiB, with `Content-Type: application/json`.
- Unknown fields are rejected with `400 invalid`.
- Errors are `{"error": {"code", "message", "hint"}}`.
- Mutations need the `X-CSRF-Token` header; the cookie is never accepted.
- A locked console (no password, no explicit opt-out) answers `503
  auth_not_configured` on `/api/v1` and the legacy console.
- Responses carry `Cache-Control: no-store` and the browser security headers.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/session` | Mode (`locked`, `password`, `disabled`), authentication state, CSRF token. |
| POST | `/api/v1/session` | Sign in with `{"password"}`. `401` wrong password, `429` throttled. |
| DELETE | `/api/v1/session` | Sign out. |
| GET | `/api/v1/cameras` | Registered cameras with adoption and PTZ status. |
| GET | `/api/v1/cameras/{cam}/state` | Full snapshot: camera, `ptz`, `streams`, `presets`, `health`, `frigate`, `media`, `endpoints`, `server`. |
| GET | `/api/v1/cameras/{cam}/health` | Plain-language checks, each with `id`, `label`, `status` (`ok`, `warn`, `bad`), `detail`, `actions`. |
| GET | `/api/v1/cameras/{cam}/snapshot.jpg` | Latest JPEG. `503` until one exists. |
| POST | `/api/v1/cameras/{cam}/ptz/move` | `{mode: relative \| absolute \| continuous, pan, tilt, zoom, speed}`; values in -1..1 (absolute zoom 0..1). `202`, or `409 ptz_busy` / `ptz_unavailable`. |
| POST | `/api/v1/cameras/{cam}/ptz/center` | `{x, y, zoom?}` in image coordinates. `409 not_calibrated` without a field-of-view calibration. |
| POST | `/api/v1/cameras/{cam}/ptz/stop` | Stop further steps and refresh the position. `202`. |
| POST | `/api/v1/cameras/{cam}/ptz/home` | Go to the preset named Home. `404 no_home` if none. |
| POST | `/api/v1/cameras/{cam}/ptz/home/save` | Save the current view as Home. |
| POST | `/api/v1/cameras/{cam}/ptz/recover` | Recover a stuck PTZ state. Uses the `recover_hook` or backend `recover_ptz()` when installed (the watchdog seam), otherwise a position poll. |
| GET | `/api/v1/cameras/{cam}/presets` | Presets with `token`, `name`, `home`, `position`, `thumbnail`. |
| POST | `/api/v1/cameras/{cam}/presets` | `{name}` saves the current view. `201`. |
| PUT | `/api/v1/cameras/{cam}/presets/{token}` | Update the preset to the current view. |
| DELETE | `/api/v1/cameras/{cam}/presets/{token}` | Delete a preset. |
| POST | `/api/v1/cameras/{cam}/presets/{token}/goto` | Go to a preset. `202`. |
| GET | `/api/v1/cameras/{cam}/presets/{token}/thumbnail.jpg` | Thumbnail captured when the preset was saved. |
| GET | `/api/v1/cameras/{cam}/frigate` | `{base_url, configured}`. |
| PUT | `/api/v1/cameras/{cam}/frigate` | `{base_url}` or `{base_url: null}`. |
| PUT | `/api/v1/cameras/{cam}/tracks/{name}` | `{codec}`: `h264`, `h265` or `mjpg`. |
| GET | `/api/v1/logs?limit=&level=&before=` | Newest first; `limit` 1-500. |
| GET | `/api/v1/events?camera=&log_level=` | Server-sent events (see below). `503 busy` above 16 clients. |

## Server-sent events

`GET /api/v1/events` sends a `state` event first, then:

- `ptz`: whenever the PTZ status, position or error changes (checked every 100 ms).
- `log`: one per new record at or above `log_level`; `id:` is the log sequence,
  and `Last-Event-ID` resumes.
- `preset`: the full preset list after a change.
- `adoption`: when the camera is adopted or released.
- `streams` and `health`: every two seconds, sent when they change.
- `session`: `{authenticated: false}` when the session ends. The stream then closes.

Heartbeat comments are sent every 15 seconds.

## Limits

- Continuous motion is a sequence of bounded steps, one in flight at a time.
  A second request while one is running gets `409`.
- Thumbnails: at most 64, each at most 2 MiB.
- Request sockets time out after 30 seconds of inactivity.
- Login: five free failures per client address, then exponential lockout up to
  five minutes.
