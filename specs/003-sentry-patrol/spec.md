# Sentry Patrol Mode

Status: implemented.
Design notes: `specs/003-sentry-patrol/` and the contract in
[`contracts/http-api.md`](contracts/http-api.md).

## User outcome

An operator can put a PTZ camera into a repeating patrol of named positions —
a "sentry" — and know that the moment something is detected, the camera stops
sweeping and lets the autotracker take over. When the tracker lets go, the
patrol resumes where it left off instead of restarting.

Each camera gets its own points, cadence and speed, so a yard camera and a
driveway camera can run different tours at the same time.

## Scope

| Area | Decision |
|---|---|
| Where the patrol runs | In Osprey, not Frigate. Osprey owns the PTZ transport, the preset table and the arbitration between people and machines, so it is the only place a patrol can be serialised safely against manual and autotracking moves. |
| What a point is | A named camera preset (the same table Frigate's `return_preset` already uses). |
| Cadence | `interval_sec` between moves, plus an optional per-point `dwell_sec` that overrides it for that point. |
| Speed | `speed` on the G5 scale (1..`ptz.max_speed`), applied to every patrol move. |
| Multiple cameras | Each camera has its own patrol, its own points, its own cadence and its own speed. Patrols run concurrently. |
| Yield rule | A patrol never moves the camera while an autotracker is active, and never moves it while a person holds manual control. |
| Control surfaces | Config file, REST API, operator web UI, and MQTT. |
| Frigate side | None required. Frigate's ONVIF autotracking is already the thing that picks up; the patrol just has to stop fighting it. |

## Behaviour

### The tour

A patrol holds an ordered list of points. Every `interval_sec` (or the point's
own `dwell_sec`) it moves to the next point and waits. It rotates back to the
first point after the last one, so the tour is endless.

The first move is deliberately *not* immediate: enabling a patrol waits one
interval before the first move, so switching it on never jerks the camera.

### Yielding

Before every move the patrol checks three things, in this order:

1. **Is an autotracker active?** Frigate's ONVIF autotracking arrives at Osprey
   as ONVIF PTZ commands from an autotrack principal. Osprey remembers when it
   last saw one. While that memory is fresh the patrol is `tracking` and waits.
2. **Is a person in control?** A manual command (console, MQTT, or a non-autotrack
   ONVIF user) starts a manual hold. During the hold the patrol is `busy` and waits.
3. **Is the camera idle?** If the head is still moving from something else, the
   patrol waits.

The check is repeated *inside* the movement lease, immediately before the move
goes out. A predicate check alone cannot close the race where Frigate starts
tracking between the check and the command; the shared lease does.

### Resuming

The patrol keeps its position in the tour across a yield. When tracking ends the
patrol waits out the remainder of the current dwell and then continues with the
*next* point — it does not jump back to the start.

### Point names

Point names refer to named presets. A name that does not exist on the camera is
reported as an error and the tour does not advance past it, so a typo cannot
silently spin the camera.

## Acceptance checks

- A patrol rotates its points in order and honours a per-point dwell
  (`test_patrol_rotates_named_points_and_uses_dwell`).
- A patrol does not move while the autotracker is active, and resumes with the
  next point afterwards (`test_sentry_patrol_defers_to_autotracking`).
- A patrol does not move while a person holds manual control, and does not
  consume the point (`test_sentry_patrol_defers_to_manual_control`).
- A patrol move that races another producer is rejected by the shared lease and
  the point is not consumed (`test_patrol_moves_are_serialised_by_the_lease`).
- A missing preset name is an error, not a silent skip
  (`test_unknown_point_is_reported_as_an_error`).
- The patrol's own moves are not mistaken for autotracking
  (`test_patrol_moves_are_not_autotracking`).
- Two cameras run independent patrols with different points and cadences
  (`test_manager_runs_independent_patrols_per_camera`).
- Malformed config is rejected before any socket is opened
  (`test_validate_sentry_rejects_malformed_settings`).

## Known limits

- Zones in Frigate are defined in frame coordinates, so a zone named after a
  place (for example a yard) only refers to that place while the camera is
  parked at the position the zone was drawn for. Autotracking therefore has to
  be gated on a full-frame zone while a patrol is in use; see
  [`docs/sentry.md`](../../docs/sentry.md).
- The patrol runs inside Osprey, so it stops when Osprey stops. There is no
  on-camera fallback.
- A camera that is not adopted yet has no preset table, so a patrol for it
  waits rather than moving.
