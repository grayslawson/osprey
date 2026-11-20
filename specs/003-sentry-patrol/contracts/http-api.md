# Sentry HTTP contract (`/api/v1`)

Owner: `osprey/webapi.py`. The routes follow the conventions in
[`../002-operator-web-ui/contracts/http-api.md`](../002-operator-web-ui/contracts/http-api.md):
JSON bodies at most 4 KiB, unknown fields rejected with `400 invalid`, errors as
`{"error": {"code", "message", "hint"}}`, mutations require the `X-CSRF-Token`
header.

Sentry is camera-scoped, so the per-camera routes live under
`/api/v1/cameras/{cam}/sentry`. The overview route is unscoped because it
reports every camera's patrol at once.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/sentry` | Overview: every camera's patrol state and the movement gate. |
| GET | `/api/v1/cameras/{cam}/sentry` | One camera's patrol: config, live state, available presets. |
| POST | `/api/v1/cameras/{cam}/sentry` | Configure and/or enable the patrol. `200` on success. |
| DELETE | `/api/v1/cameras/{cam}/sentry` | Disable the patrol and drop its points. `200`. |

`{cam}` is `default` for a single-camera deployment or a registered camera ID.
A camera that exists but has no patrol configured answers `404 no_sentry` on the
per-camera GET, so the UI can tell "not configured" from "not adopted".

## POST body

Every field is optional; an absent field keeps its current value. `points`
replaces the whole list when present.

```json
{
  "enabled": true,
  "interval_sec": 45,
  "speed": 1000,
  "tracking_hold_sec": 15,
  "points": [
    {"name": "driveway", "dwell_sec": 20},
    {"name": "gate"}
  ]
}
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `false` | Whether the patrol runs. |
| `interval_sec` | number | `45` | Seconds between moves. At least 1. |
| `speed` | integer | `ptz.max_speed` | G5 speed scale for patrol moves. `1..max_speed`. |
| `tracking_hold_sec` | number | `15` | How long after the last autotracking command the patrol stays out of the way. At least 1. |
| `points` | array | `[]` | Ordered tour. Each entry is `{"name": "...", "dwell_sec": number?}`. |

Validation errors:

| Code | Cause |
|---|---|
| `400 invalid` | A field has the wrong type, an unknown field was sent, `interval_sec` or `tracking_hold_sec` is below 1, `speed` is out of range, or two points share a name. |
| `404 no_sentry` | The camera has no patrol and the request was a GET. |
| `404 unknown_camera` | The camera id does not resolve. |
| `409 ptz_unavailable` | The camera has no PTZ channel, so a patrol cannot be enabled. |

## GET body

```json
{
  "camera": "default",
  "config": {
    "enabled": true,
    "interval_sec": 45,
    "speed": 1000,
    "tracking_hold_sec": 15,
    "points": [{"name": "driveway", "dwell_sec": 20}, {"name": "gate"}]
  },
  "state": {
    "enabled": true,
    "state": "waiting",
    "point": "driveway",
    "points": ["driveway", "gate"],
    "next_move_at": 1732140000.0,
    "last_error": null,
    "moves": 12
  },
  "presets": ["driveway", "gate", "home"],
  "movement": {"busy": false, "owner": null, "acquired": 3, "rejected": 0}
}
```

`state.state` is one of:

| State | Meaning |
|---|---|
| `disabled` | The patrol is not running. |
| `waiting` | Enabled, waiting for the first move or for the next dwell. |
| `moving` | A move has just been sent; the dwell is running. |
| `tracking` | An autotracker is active, so the patrol is standing down. |
| `camera_busy` | The camera was still moving from something else. |
| `busy` | Another producer held the movement lease, or a person holds manual control. |
| `error` | The last move failed. `last_error` says why. |

`next_move_at` is a monotonic deadline. It is comparable only inside one
process; the UI treats it as a countdown and never as a wall-clock time.

The overview route returns `{"cameras": {cam: <the per-camera body above>},
"movement": {...}}`.

## Notes

- Enabling a patrol does not move the camera immediately. The first move
  happens one `interval_sec` later, so an operator can enable it and back away.
- `POST` with `points` that name presets the camera does not have is accepted.
  The patrol reports the unknown point as `last_error` when it reaches it,
  because a preset can be added after the patrol is configured.
- The patrol never holds the camera against a person: a manual command stops
  patrol moves for `ptz.manual_hold_sec`, exactly as it stops autotracking.
