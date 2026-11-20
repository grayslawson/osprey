# Sentry patrol mode

Sentry puts a PTZ camera on a repeating patrol: it sweeps through a list of
named positions on a timer, so a camera that would otherwise stare at one spot
covers more ground. The moment something is detected, the patrol steps aside
and lets the autotracker take over. When the tracker lets go, the patrol picks
up where it left off.

It is called sentry because that is what the camera is doing: walking a beat,
not standing still.

## What it does, precisely

A patrol holds an ordered list of points. A point is a **named preset** — the
same preset table Frigate's `return_preset` already uses, so anything you can
name in the operator console you can put in a tour.

Every `interval_sec` the patrol moves to the next point and waits there. After
the last point it starts again at the first, so the tour is endless. A point
may carry its own `dwell_sec`, which overrides `interval_sec` for that point
only — useful for the spot that deserves a longer look.

Enabling a patrol does **not** move the camera straight away. The first move
happens one interval later, so you can switch it on and step back.

### Yielding is the whole point

Before every move, the patrol checks three things:

1. **Is an autotracker driving?** Frigate's ONVIF autotracking arrives at
   Osprey as ONVIF PTZ commands. Osprey remembers when it last saw one, and
   while that memory is fresh the patrol stands down.
2. **Is a person in control?** A manual command — console, MQTT, or an ONVIF
   user that is not an autotracker — starts a manual hold. During the hold the
   patrol stands down too.
3. **Is the camera idle?** If the head is still moving from something else, the
   patrol waits.

The same checks run again inside the movement lease, immediately before the
command goes out. A check on its own cannot close the window where Frigate
starts tracking between the check and the command; the shared lease does.

The patrol keeps its place in the tour across a yield. When tracking ends it
waits out the rest of the current dwell and continues with the **next** point.
It does not jump back to the start.

### Several cameras, several tours

Each camera has its own points, cadence and speed, and each runs its own
patrol. A yard camera and a driveway camera can sweep different routes at the
same time without getting in each other's way: the movement lease is held per
camera.

## Configuration

Sentry is off by default. It is configured in the `sentry` block of
`osprey.json` (or `cuckoo.json` on an older install):

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
| `enabled` | `false` | Master switch. A camera inherits it. |
| `interval_sec` | `45` | Seconds between moves. At least 1. |
| `speed` | `1000` | PTZ speed for patrol moves, on the G5 scale (1..`ptz.max_speed`). |
| `tracking_hold_sec` | `15` | How long after the last autotracking command the patrol keeps out of the way. At least 1. |
| `cameras` | `{}` | Per-camera settings, keyed by camera MAC. |

A camera's entry may override any of `enabled`, `interval_sec`, `speed` and
`tracking_hold_sec`, and must supply `points`. A MAC is the key because an
address can change and a camera's identity should not; write it with or
without separators (`AABBCCDDEEFF` and `aa:bb:cc:dd:ee:ff` are the same
camera, and naming one twice is an error).

The MAC is the camera's own, as shown by `osprey doctor` and in the operator
console's camera list.

## Frigate: the zone caveat

This is the part that surprises people, so read it before enabling a patrol.

Frigate zones are defined in **frame coordinates**. A zone called `yard` means
"the region at the bottom of the picture" — which only corresponds to the
actual yard while the camera is parked at the position the zone was drawn
for. Move the camera and the zone moves with it, covering different ground.

Frigate's ONVIF autotracking is gated on `required_zones`. If that list names
`yard`, then a detection outside the bottom of the current frame never
triggers autotracking — including the detection that a patrol sweep just
brought into view. The patrol would swing the camera around, the object would
be plainly visible, and nothing would happen.

So while a patrol is in use, autotracking has to be gated on a **full-frame**
zone. Define one on the camera and point `required_zones` at it:

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

Frigate requires `required_zones` to be non-empty when autotracking is on, so
the zone has to exist even if it covers the whole frame.

The `review` zones are a separate matter and usually want to stay as they are:
`review.alerts.required_zones` decides which detections become alerts, and
that is a question about where the object is, not about whether the camera
should follow it. Be aware that while the camera is away from home, a
place-named zone describes a different part of the world.

### The return-home interaction

Frigate's `return_preset` sends the camera back to a preset when it loses the
object. On a camera that is patrolling, that means the camera leaves its patrol
position and goes home, and the patrol then continues with its next point.
That is normal, and it is why `tracking_hold_sec` exists: it keeps the patrol
out of the way for a moment after the tracker lets go, rather than the two
fighting over the camera.

## Operator console

The **Patrol** page shows the selected camera's patrol: whether it is
configured, whether it is running, the live state, the current point and the
move count. From there you can enable or disable it, set the cadence, the
speed and the tracking hold, and build the tour by adding, removing and
reordering points. Point names come from the camera's presets, with free text
allowed so a preset can be added later.

The states you will see:

| State | Meaning |
|---|---|
| `disabled` | The patrol is not running. |
| `waiting` | Enabled, waiting for the next move. |
| `moving` | A move has just gone out; the dwell is running. |
| `tracking` | An autotracker is driving; the patrol is standing down. |
| `camera_busy` | The camera was still moving from something else. |
| `busy` | Another producer held the movement lease, or a person holds manual control. |
| `error` | The last move failed. The reason is shown next to it. |

## REST API

Served on the ONVIF port under `/api/v1`, with the same session cookie and
`X-CSRF-Token` rules as the rest of the operator API. The full contract is in
[`specs/003-sentry-patrol/contracts/http-api.md`](../specs/003-sentry-patrol/contracts/http-api.md).

```
GET    /api/v1/sentry                    every camera's patrol
GET    /api/v1/cameras/{cam}/sentry      one camera's patrol
POST   /api/v1/cameras/{cam}/sentry      configure and/or enable
DELETE /api/v1/cameras/{cam}/sentry      disable and forget the patrol
```

```sh
# What is the patrol doing right now?
curl -s --cookie cookies.txt http://192.0.2.10:8000/api/v1/sentry

# Start a tour.
curl -s --cookie cookies.txt -H "X-CSRF-Token: $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"enabled":true,"interval_sec":45,"points":[{"name":"driveway"},{"name":"gate"}]}' \
     http://192.0.2.10:8000/api/v1/cameras/default/sentry
```

## MQTT

With MQTT enabled, the patrol can be driven and watched over the broker:

```
osprey/<device>/command/sentry   {"camera":"AABBCCDDEEFF","enabled":true,
                                  "points":[{"name":"driveway"}]}
osprey/<device>/state/sentry      the patrol state
```

The command takes the same fields as the REST body, plus `camera` — a camera
MAC or a display name. Every field except `camera` is optional, and an absent
field keeps its current value.

Unlike the movement commands, the sentry command does **not** take the
movement lease and does **not** start a manual hold: it configures a patrol
rather than moving the camera, and it must not hold up a move in progress.

## Known limits

- The patrol runs inside Osprey, so it stops when Osprey stops. There is no
  on-camera fallback.
- A camera that has not been adopted yet has no preset table, so a patrol for
  it waits rather than moving.
- A point naming a preset that does not exist is reported as an error when the
  patrol reaches it. The tour does not skip it, so a typo cannot make the
  camera spin through a shortened route.
- Frigate zones are frame-relative, as described above. A place-named zone
  only means that place while the camera is at home.
- The first move is delayed by one interval after the patrol is enabled, and
  changing the configuration restarts the tour from the first point.
