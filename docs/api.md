# API reference

Osprey has four network surfaces. This page covers the HTTP ones that you
script against. ONVIF is a standard, so the table at the end only lists where
Osprey serves it.

| Surface | Port | Auth |
|---|---|---|
| Operator API, `/api/v1` | 8000 | Console session and CSRF token |
| `/healthz`, `/metrics` | 8000 | None |
| ONVIF services | 8000 (8001 for the media persona) | WS-Security, if enabled |
| RTSP | 8554 | Digest, if enabled |

There is no OpenAPI file. The routes below come from `osprey/webapi.py`, and
the tests in `osprey/tests/test_webapi.py` and `test_webapi_sentry.py` check
them.

## Operator API

### Sign in

Every `/api/v1` route needs a session, except `GET` and `POST` on
`/api/v1/session`. A locked console, with no admin password set, answers `503
auth_not_configured`. See [authentication](authentication.md).

```sh
curl -s --cookie-jar cookies.txt -H 'Content-Type: application/json' \
     -d '{"password":"ADMIN_PASSWORD"}' http://192.0.2.10:8000/api/v1/session
```

The response has `mode`, `authenticated`, `principal`, `csrf` and `ingress`.
Send the `csrf` value in an `X-CSRF-Token` header on every `POST`, `PUT` and
`DELETE`. The cookie alone is never enough.

```sh
curl -s --cookie cookies.txt http://192.0.2.10:8000/api/v1/cameras

curl -s --cookie cookies.txt -H "X-CSRF-Token: $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"mode":"relative","pan":0.1,"tilt":0,"zoom":0}' \
     http://192.0.2.10:8000/api/v1/cameras/default/ptz/move
```

### Conventions

- Bodies are JSON, at most 4 KiB, with `Content-Type: application/json`.
- Unknown fields are rejected with `400 invalid`.
- Errors look like `{"error": {"code", "message", "hint"}}`.
- Responses carry `Cache-Control: no-store`, apart from preset thumbnails.
- `{cam}` is `default` for a single camera deployment, or a camera ID from
  `GET /api/v1/cameras`. An unknown ID returns `404`.
- Five failed sign ins from one address start an exponential lockout of up
  to five minutes, answered with `429` and `Retry-After`.
- A PTZ command from the API counts as a person for arbitration, so it holds
  Frigate off for `ptz.manual_hold_sec`. See
  [Frigate integration](frigate-integration.md#manual-control-and-autotracking).

### Routes

| Method | Path | What it does |
|---|---|---|
| GET | `/api/v1/session` | Auth mode (`locked`, `password` or `disabled`), whether you are signed in, and the CSRF token. |
| POST | `/api/v1/session` | Sign in with `{"password"}`. `401` for a wrong password, `429` when throttled. |
| DELETE | `/api/v1/session` | Sign out. |
| GET | `/api/v1/cameras` | Registered cameras with adoption and PTZ status. |
| GET | `/api/v1/cameras/{cam}/state` | One snapshot: `camera`, `ptz`, `streams`, `presets`, `health`, `frigate`, `media`, `endpoints` and `server`. |
| GET | `/api/v1/cameras/{cam}/health` | Plain language checks. Each has `id`, `label`, `status` (`ok`, `warn` or `bad`), `detail` and `actions`. |
| GET | `/api/v1/cameras/{cam}/snapshot.jpg` | The latest JPEG. `503` until one exists. |
| POST | `/api/v1/cameras/{cam}/ptz/move` | Move the head. See below. |
| POST | `/api/v1/cameras/{cam}/ptz/center` | `{x, y, zoom?}` in image coordinates. `409 not_calibrated` on a model without a field of view calibration. |
| POST | `/api/v1/cameras/{cam}/ptz/stop` | Stop further steps and refresh the position. `202`. |
| POST | `/api/v1/cameras/{cam}/ptz/home` | Go to the preset named `home`. `404 no_home` if there is none. |
| POST | `/api/v1/cameras/{cam}/ptz/home/save` | Save the current view as `home`. |
| POST | `/api/v1/cameras/{cam}/ptz/recover` | Recycle a stuck PTZ channel. Same as **Recover PTZ** in the console. |
| GET | `/api/v1/cameras/{cam}/presets` | Presets with `token`, `name`, `home`, `position` and `thumbnail`. |
| POST | `/api/v1/cameras/{cam}/presets` | `{"name"}` saves the current view. `201`. |
| PUT | `/api/v1/cameras/{cam}/presets/{token}` | Update the preset to the current view. |
| DELETE | `/api/v1/cameras/{cam}/presets/{token}` | Delete a preset. |
| POST | `/api/v1/cameras/{cam}/presets/{token}/goto` | Go to a preset. `202`. |
| GET | `/api/v1/cameras/{cam}/presets/{token}/thumbnail.jpg` | The thumbnail taken when the preset was saved. |
| GET | `/api/v1/cameras/{cam}/frigate` | `{base_url, configured}`. |
| PUT | `/api/v1/cameras/{cam}/frigate` | `{"base_url"}`, or `{"base_url": null}` to clear it. |
| PUT | `/api/v1/cameras/{cam}/tracks/{name}` | `{"codec"}`: `h264`, `h265` or `mjpg`. Osprey arms the track again without a restart. |
| GET | `/api/v1/logs?limit=&level=&before=` | Redacted log records, newest first. `limit` is 1 to 500. |
| GET | `/api/v1/events?camera=&log_level=` | Server sent events. `503 busy` above 16 clients. |
| GET | `/api/v1/sentry` | Every camera's patrol and the movement gate. |
| GET | `/api/v1/cameras/{cam}/sentry` | One patrol: config, live state and available presets. |
| POST | `/api/v1/cameras/{cam}/sentry` | Configure or enable the patrol. |
| DELETE | `/api/v1/cameras/{cam}/sentry` | Disable the patrol and drop its points. |
| POST | `/api/v1/cameras/{cam}/sentry/hold` | `{"seconds": 1-300, "reason"?}` stands the patrol down. It resumes on its own when the time is up. |
| DELETE | `/api/v1/cameras/{cam}/sentry/hold` | End the hold now. |
| POST | `/api/v1/cameras/{cam}/sentry/dwell` | `{"dwell_sec": {"gate": 60}, "ttl_sec": 1-86400}` overrides per point dwell for a while. |
| DELETE | `/api/v1/cameras/{cam}/sentry/dwell` | Drop the override. |
| GET, POST, DELETE | `/api/v1/assist/...` | The console's door to [Osprey Assist](#osprey-assist-routes). `404 no_assist` unless Assist is switched on. |

### `ptz/move`

```json
{"mode": "relative", "pan": 0.1, "tilt": -0.1, "zoom": 0, "speed": 0.5}
```

`mode` is `relative`, `absolute` or `continuous`. `pan` and `tilt` run from
-1 to 1, and `zoom` from -1 to 1 (0 to 1 when `absolute`). `speed` is optional
and runs from 0 to 1, as a fraction of `ptz.max_speed`. The answer is `202`,
or `409 ptz_busy` or `ptz_unavailable`. The camera has no continuous mode, so
`continuous` becomes one bounded step, and a held joystick sends the next one
as each step completes. A second request while a step is running gets `409`.

### Sentry bodies

`POST .../sentry` takes any of these fields. A missing field keeps its
current value, and `points` replaces the whole list.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | Whether the patrol runs. |
| `interval_sec` | number | `45` | Seconds between moves, at least 1. |
| `speed` | integer | `1000` | G5 speed, 1 to 10000. |
| `tracking_hold_sec` | number | `15` | How long the patrol waits after the last autotracking command, at least 1. |
| `points` | array | `[]` | The tour, as `{"name", "dwell_sec"?}` entries. |

Enabling a patrol does not move the camera at once. The first move comes one
`interval_sec` later. `state.state` is one of `disabled`, `waiting`, `moving`,
`tracking`, `camera_busy`, `busy` or `error`, as [sentry](sentry.md#how-a-patrol-runs)
describes. Errors on this route: `400 invalid` for a wrong type, an unknown
field or a duplicate point name, `404 no_sentry` for a camera with no patrol,
and `409 ptz_unavailable` for a camera without a PTZ channel.

The hold and the dwell override live in memory. They never touch
`sentry.json`, they expire by themselves, and a dwell is clamped to 1 second up
to ten times the interval. Unknown point names are ignored. Both return
`200` with `held: false` or an empty `applied` when the camera has no running
patrol.

`GET .../state` also carries two things for Assist. `ptz.scene` is `DAY` or
`NIGHT` as the camera last reported it, and `null` until it does.
`ptz.fov` is the camera's measured geometry (`pan_degrees`, `tilt_degrees` and the
wide and tele fields of view), or `null` when the model is not calibrated.
`assist_signals` has the scene, `auto_active_age_sec` (seconds since Frigate
last moved the camera, `null` if never), `manual_hold_remaining_sec`,
`patrol_hold_remaining_sec` and `lease_owner`.

### Osprey Assist routes

Osprey Assist is a separate process, see [Assist](assist.md). It authenticates
with `Authorization: Bearer <token>`, the token in `OSPREY_ASSIST_TOKEN_FILE`.
A bearer request needs no cookie and no CSRF header, and each move it makes is
tagged `ASSIST`: it yields to a person and to Frigate, never starts a manual
hold and never counts as autotracking. It takes the same movement lease as the
patrol, so the answer to a move can be `409 ptz_busy`. A move dropped because a
person holds the camera is refused with `409` rather than reported as done.

The token may call only these routes. Anything else is `403 assist_scope`.

| Scope | Routes |
|---|---|
| `quality` | `GET` cameras, events, `state`, `snapshot.jpg`, `presets`, `sentry`. `POST` `ptz/move`, `ptz/center`, `presets/{token}/goto`, `isp`, `sentry/hold`, `sentry/dwell`. `DELETE` `sentry/hold`, `sentry/dwell`. |
| `presets` | `POST presets`, `PUT presets/{token}`, and `POST sentry` with the same points and a changed `night_preset`. |
| `patrol` | `POST sentry` with a different list of points and an `enabled` flag (nothing else about the patrol), and `DELETE presets/{token}`. Survey uses this. |

`isp` from the token accepts only `hdr`, `gain`, `shutter`, `aeTargetPercent`,
`brightness`, `contrast`, `sharpness`, `denoise`, `icrSensitivity`,
`touchFocusX` and `touchFocusY`, as numbers inside fixed bounds. The keys the
camera ignores (`focusPosition`, `irLedLevel`, `irLedMode`, `wdr`) are refused.
The console's own `isp` route is unchanged.

The console reaches Assist through `/api/v1/assist/...`. Osprey checks the
console session and CSRF token first, then forwards a short list of paths
(`status`, `settings`, `decisions`, `frames`, `check`, `data` and the `survey/` routes) to
`assist.url` with the token and an `X-Osprey-Principal: console` header.
`502 assist_unreachable` means Assist did not answer.

### Server sent events

`GET /api/v1/events` sends a `state` event first, then:

- `ptz` when the status, position, scene or error changes (checked every 100 ms).
- `log` for each new record at or above `log_level`. The event `id` is the log
  sequence, and `Last-Event-ID` resumes.
- `preset` with the full list, after a change.
- `adoption` when the camera is adopted or released.
- `streams` and `health` every two seconds, when they change.
- `session` with `{authenticated: false}` when the session ends, and then the
  stream closes.

A heartbeat comment arrives every 15 seconds.

### Limits

- Thumbnails: at most 64, each at most 2 MiB.
- Request sockets time out after 30 seconds of inactivity.

## Health and metrics

`GET /healthz` and `GET /metrics` need no login. Use `/cameras/{camera-id}/healthz`
on a multi camera setup. The fields, the Prometheus metric names and the log
lines worth alerting on are in [PTZ health](ptz-health.md#health-endpoint).

## ONVIF and RTSP

| Service | Path or URL |
|---|---|
| Device | `http://HOST:8000/onvif/device_service` |
| Media | `http://HOST:8000/onvif/media_service` |
| PTZ | `http://HOST:8000/onvif/ptz_service` |
| Imaging | `http://HOST:8000/onvif/imaging_service` |
| Events | `http://HOST:8000/onvif/events_service` |
| RTSP | `rtsp://HOST:8554/video1`, `video2` and `video3` (the tracks you arm) |

The media persona serves the same paths on its own port, without PTZ.
MQTT commands and state are documented in [MQTT](mqtt.md).
