# Sentry patrol

Sentry moves an idle PTZ camera through a list of presets on a timer, so it
covers more ground than one fixed view. When Frigate starts autotracking or a
person takes control, the patrol stands down. When they let go, it carries on
from where it was.

## How a patrol runs

A patrol is an ordered list of points. Each point is a named preset, from the
same table Frigate's `return_preset` uses. Save the presets first, on the
console's **Live** tab, then build the patrol on the **Patrol** tab.

Every `interval_sec` the patrol moves to the next point. After the last point
it starts again at the first. A point can set its own `dwell_sec` to stay
longer than `interval_sec`.

Enabling a patrol does not move the camera at once. The first move comes one
interval later.

### When it stands down

Before each move the patrol checks three things, and waits if any is true:

1. An autotracker sent an ONVIF PTZ command within the last
   `tracking_hold_sec`.
2. A person holds the camera. A command from the console, MQTT, or an ONVIF
   user that is not an autotracker starts a manual hold of
   `ptz.manual_hold_sec`.
3. The camera is still moving from another command.

The same checks run again inside the per-camera movement lease, just before
the command goes out, so Frigate cannot slip in between the check and the
move.

A patrol move leaves the head travelling for a few seconds. If an
autotracking command arrives in that window, it replaces the patrol move and
the patrol stands down. Without this, the first tracking command of a
detection would be refused as "camera already moving".

After a yield the patrol waits out the current dwell and goes on to the next
point. It does not restart the tour.

Each camera has its own points, cadence and speed, and its own lease, so
several cameras patrol independently.

## Configuration

Sentry is off by default. The console is the simplest way to set it up. To
keep it in your own config file instead (see
[installation](installation.md#your-own-configuration-file)), use the
`sentry` block of `osprey.json`:

```json
{
  "sentry": {
    "enabled": true,
    "interval_sec": 45,
    "speed": 1000,
    "tracking_hold_sec": 15,
    "cameras": {
      "AABBCCDDEEFF": {
        "points": [
          {"name": "driveway", "dwell_sec": 20},
          {"name": "gate"},
          {"name": "home"}
        ]
      }
    }
  }
}
```

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch, inherited by each camera. |
| `interval_sec` | `45` | Seconds between moves. At least 1. |
| `speed` | `1000` | Patrol speed on the G5 scale, 1 to 10000. Values above 1000 are unmeasured on hardware. |
| `tracking_hold_sec` | `15` | Seconds after the last autotracking command before the patrol moves again. At least 1. |
| `cameras` | `{}` | Per-camera settings, keyed by camera MAC. |

A camera entry must list `points` and may override `enabled`,
`interval_sec`, `speed` and `tracking_hold_sec`. The key is the camera's MAC,
because an address can change. Separators are optional: `AABBCCDDEEFF` and
`aa:bb:cc:dd:ee:ff` name the same camera, and naming it twice is an error. The
operator console's camera list shows each MAC.

### Runtime changes win

Changes made in the console, the API or MQTT are saved to `sentry.json` next
to the control certificate in the state directory, so a running patrol comes
back after a restart. A camera listed in `sentry.json` uses that entry; any
other camera uses `osprey.json`. Removing a patrol through the API records the
removal, so it stays removed even if `osprey.json` still lists it.

To hand a camera back to `osprey.json`, delete its entry from `sentry.json`.

## Frigate zones and autotracking

Frigate zones are drawn in frame coordinates. A zone named `yard` covers part
of the picture, and that part is only the yard while the camera is at the
position the zone was drawn for.

Frigate starts autotracking only for objects in `required_zones`. If that
names a place-shaped zone, a patrol can swing the camera onto an object that
never triggers tracking. While a patrol is in use, gate autotracking on a
zone that covers the whole frame:

```yaml
cameras:
  ptz:
    zones:
      ptz_viewport:
        friendly_name: Full PTZ viewport
        coordinates: 0,0,1,0,1,1,0,1
    onvif:
      autotracking:
        enabled: true
        required_zones:
          - ptz_viewport
```

Frigate needs a non-empty `required_zones` when autotracking is on, so the
zone has to exist. `review.alerts.required_zones` is a separate setting and can
usually stay as it is.

Frigate's `return_preset` sends the camera to a preset when it loses the
object. On a patrolling camera that preset replaces the current patrol
position, and the patrol then moves on to its next point. `tracking_hold_sec`
keeps the two from fighting right after tracking ends.

## Operator console

The **Patrol** page shows the selected camera's patrol: whether it is
configured and running, its state, the current point and the move count. You
can enable or disable it, set the interval, speed and tracking hold, and add,
remove and reorder points. Point names come from the camera's presets, and
free text is accepted for a preset you will add later.

| State | Meaning |
|---|---|
| `disabled` | Not running. |
| `waiting` | Enabled, waiting for the next move. |
| `moving` | A move went out; the dwell is running. |
| `tracking` | An autotracker is driving; the patrol is standing down. |
| `camera_busy` | The camera was still moving from another command. |
| `busy` | Another producer held the movement lease, or a person holds the camera. |
| `error` | The last move failed. The reason is shown beside it. |

## REST API

Served on the ONVIF port under `/api/v1`, with the same session cookie and
`X-CSRF-Token` rules as the rest of the [operator API](operator-ui.md). The
full route list is in the [API reference](api.md).

```text
GET    /api/v1/sentry                    every camera's patrol
GET    /api/v1/cameras/{cam}/sentry      one camera's patrol
POST   /api/v1/cameras/{cam}/sentry      configure, enable or disable
DELETE /api/v1/cameras/{cam}/sentry      disable and forget the patrol
```

Sign in first. The response's `csrf` value is the `$TOKEN` below:

```sh
curl -s --cookie-jar cookies.txt -H 'Content-Type: application/json' \
     -d '{"password":"ADMIN_PASSWORD"}' http://192.0.2.10:8000/api/v1/session

curl -s --cookie cookies.txt http://192.0.2.10:8000/api/v1/sentry

curl -s --cookie cookies.txt -H "X-CSRF-Token: $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"enabled":true,"interval_sec":45,"points":[{"name":"driveway"},{"name":"gate"}]}' \
     http://192.0.2.10:8000/api/v1/cameras/default/sentry
```

`default` is the primary camera. With several cameras, use the IDs from
`GET /api/v1/cameras`.

## MQTT

The `command/sentry` topic takes the same fields as the REST body plus
`camera`, a MAC or display name. State is published on `state/sentry`. See
[MQTT](mqtt.md).

## Known limits

- The patrol runs inside Osprey and stops when Osprey stops. The camera has
  no fallback of its own.
- A camera that is not adopted yet has no presets, so its patrol waits.
- A point that names a missing preset is reported as an error when the patrol
  reaches it. The tour does not skip it, so a typo cannot quietly shorten the
  route.
- Changing a patrol's configuration restarts its tour at the first point.
