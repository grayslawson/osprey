# Implementation Plan: Sentry Patrol Mode

**Branch**: `003-sentry-patrol`
**Date**: 2026-11-20
**Spec**: [spec.md](spec.md)

## Summary

Osprey already ships the *engine* for an idle PTZ patrol (`osprey/sentry.py`,
`osprey/sentry_runtime.py`) with a movement gate and a set of unit tests, but
nothing ever constructs it. The patrol exists as a library and is absent from
the product.

This plan wires the engine into the running controller and gives it the four
surfaces an operator expects: a config file, a REST API, the operator web UI
and MQTT. It also makes the patrol safe to run against a live autotracker,
which is the part the current library cannot do on its own: the library can
*ask* whether tracking is active but nothing in Osprey can answer that
question, and the library's own moves are tagged as autotracking, so a patrol
would mistake itself for Frigate and never advance.

## Technical Context

**Language/Version**: Python 3.11+ application code; no-build Preact/htm web app
**Primary Dependencies**: Existing Osprey modules only — `arbitration`, `controller`,
`presets`, `sentry`, `sentry_runtime`, `webapi`, `mqtt_bridge`. No new runtime dependency.
**Storage**: Named presets already persist in `osprey-presets.json`; the patrol
configuration persists in the operator config file
**Testing**: pytest, mypy, ruff
**Target Platform**: Linux hosts and containers; Home Assistant and NixOS packaging consume the
same runtime contracts
**Project Type**: Network service with ONVIF/RTSP surfaces and a browser console
**Performance Goals**: The patrol scheduler wakes four times a second and does no
work unless a move is due
**Constraints**: Frigate stays external; a patrol must never fight a person or an
autotracker; the movement lease is shared with every other PTZ producer
**Scale/Scope**: One controller may serve multiple cameras, each with its own patrol

## Constitution Check

*Gate: PASS before research.*

- **Safe Camera Control and Compatibility**: The patrol issues the same preset
  moves the console already issues, through the same controller and the same
  arbitration. It yields to manual control and to autotracking.
- **Explicit Contracts**: The REST contract is written down in
  `contracts/http-api.md` before the routes are added.
- **Deterministic Tests**: The scheduler is stepped with an injected clock. No
  test sleeps to observe a patrol.
- **No Secret Leakage**: The patrol config holds preset names and numbers, never
  credentials.

## The three problems this feature has to solve

### 1. Telling a patrol apart from an autotracker

`arbitration.py` has exactly two sources: `MANUAL` (a person) and `AUTO`
(an autotracker *or* the patrol). That was fine while the patrol was never
wired up. It stops being fine now, because the controller needs to know when
*Frigate* is driving so the patrol can get out of the way — and the patrol
tags its own moves `AUTO`, so it would look like Frigate to itself.

The fix is a third source, `PATROL`. It behaves like `AUTO` for arbitration
(a manual hold still stops it) but is distinguishable, so the controller can
record autotracking activity without counting the patrol's own moves.

### 2. Knowing that an autotracker is active

There is no signal from Frigate that says "I am tracking now". The signal that
does exist is the traffic itself: while Frigate's ONVIF autotracker is
following an object it keeps issuing PTZ moves. Osprey already tags those
commands `AUTO` at the ONVIF boundary and drops them into the controller.

So the controller records the time of the last `AUTO` command per camera, and
the patrol asks "has an autotracking command arrived in the last N seconds?".
`tracking_hold_sec` is the grace window: it has to be long enough to cover
the gap between two of Frigate's moves, and short enough that the patrol
resumes promptly once Frigate has genuinely let go.

### 3. Not losing a move to a race

The library's own docstring says the lease "must be shared with every other
movement producer ... checking a predicate alone cannot close a check-then-move
race". The engine already re-checks inside the lease. What it cannot do is
notice that a *manual hold started between the check and the command*, because
the controller answers "success" for a dropped command so that an autotracker
does not see an error and back off.

For the patrol that answer is wrong: it would consume a tour point without the
camera moving. The controller therefore gains a strict variant of the preset
move that reports the refusal instead of swallowing it.

## Architecture

```
config.py         validate_sentry()  ->  {enabled, interval_sec, speed, tracking_hold_sec, cameras:{mac:{...}}}
      |
main.py           builds one SentryRuntime per configured camera
      |
sentry_manager.py SentryManager: owns the runtimes, start/stop, configure, status
      |
sentry_runtime.py SentryRuntime -> SentryPatrol (existing engine, unchanged contract)
      |
controller.py     goto_preset(..., strict=True) reports a refused move
      arbitration.py  adds the PATROL source
```

### The movement gate becomes per-camera

`MovementGate` holds one lock, which is only correct for a single camera. With
one patrol per camera, camera A's move would block camera B's. The gate grows
a key: `lease(owner, key)`, where the key is the camera's canonical id and the
empty key is the default (unscoped) producer. `snapshot()` keeps its existing
shape and adds a per-key breakdown.

### The patrol asks for a preset by name

`controller.goto_preset` takes a slot index. The patrol works in names, because
that is how the operator thinks and how Frigate's `return_preset` is written.
The manager resolves a name to a slot against the camera's preset table at
move time, and refuses the move when no such preset exists.

## Deliverables

| File | Change |
|---|---|
| `osprey/arbitration.py` | Add the `PATROL` source. |
| `osprey/controller.py` | Record autotracking activity per camera; add a strict preset move. |
| `osprey/sentry_runtime.py` | Per-camera gate keys; speed on the config. |
| `osprey/sentry_manager.py` | **New.** Multi-camera manager and status. |
| `osprey/config.py` | `validate_sentry` and defaults. |
| `osprey/main.py` | Build the manager, share the gate, start and stop it. |
| `osprey/webapi.py` | `/api/v1/sentry` and `/api/v1/cameras/{cam}/sentry`. |
| `osprey/mqtt_bridge.py` | Sentry command and state topics. |
| `osprey/web/components/sentry.js` | **New.** Operator UI panel. |
| `osprey/tests/test_sentry_*.py` | Tests for each layer. |
| `docs/sentry.md` | **New.** Operator guide. |

## Verification

1. `python3 -m pytest -q osprey/tests` — unit and integration tests.
2. `python3 -m mypy --config-file osprey/mypy.ini osprey` — type check.
3. `python3 -m ruff check .` — lint.
4. `./scripts/validate-repo.sh` — repository policy.
5. `sh homeassistant/validate.sh` — add-on static checks.
6. Live: a patrol runs against the deployed camera while Frigate autotracking
   is enabled; the camera sweeps, yields to a detection, and resumes.
