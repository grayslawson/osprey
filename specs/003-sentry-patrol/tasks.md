# Tasks: Sentry Patrol Mode

**Input**: Design documents in `/specs/003-sentry-patrol/`
**Prerequisites**: `plan.md`, `spec.md`, `contracts/http-api.md`

## Phase 1: Foundations

**Purpose**: Give the patrol the two things it cannot work without — a source
of its own that a person is not, and a way to know an autotracker is driving.

- [x] T001 Add the `PATROL` arbitration source, which yields to a manual hold
      like an autotracker but is distinguishable from one, in `osprey/arbitration.py`
- [x] T002 Record the time of the last autotracking command per camera and
      expose it as `Controller.auto_active`, in `osprey/controller.py`
- [x] T003 Add a strict preset move that reports a refusal instead of
      swallowing it, so a patrol does not consume a tour point for a move that
      never went out, in `osprey/controller.py`
- [x] T004 Move the patrol's own moves from `AUTO` to `PATROL`, in `osprey/sentry.py`

**Checkpoint**: A patrol move is no longer mistaken for an autotracker move.

## Phase 2: Configuration

**Purpose**: One patrol per camera, each with its own points, cadence and speed.

- [x] T005 Add `validate_sentry` with a shared default block and a per-camera
      map keyed by camera MAC, in `osprey/config.py`
- [x] T006 Reject unknown keys, duplicate MACs, duplicate point names, empty
      names and out-of-range values before any socket is opened
- [x] T007 Carry the block from the config file into `Options`, in `osprey/main.py`

**Checkpoint**: A malformed patrol is a startup error, not a runtime surprise.

## Phase 3: The runtime

**Purpose**: Run the patrol for real, next to everything else that moves the camera.

- [x] T008 Key the movement gate per camera, so one patrol cannot block another,
      in `osprey/sentry_runtime.py`
- [x] T009 Add the patrol speed and the tracking hold to the patrol config,
      in `osprey/sentry_runtime.py`
- [x] T010 Add `SentryManager`: one patrol per camera, independent tours,
      enable, disable, remove and status, in `osprey/sentry_manager.py`
- [x] T011 Resolve a patrol point name to a preset slot and refuse a move the
      camera will not take, in `osprey/main.py`
- [x] T012 Share the one movement gate between the console, MQTT, autotracking
      and the patrol, in `osprey/main.py`

**Checkpoint**: The patrol runs, and it stands down for a person or a tracker.

## Phase 4: The surfaces

**Purpose**: Let an operator start, stop and watch a patrol without editing JSON.

- [x] T013 Add `GET /api/v1/sentry` and `GET|POST|DELETE
      /api/v1/cameras/{cam}/sentry`, in `osprey/webapi.py`
- [x] T014 Add the `command/sentry` and `state/sentry` MQTT topics, in
      `osprey/mqtt_bridge.py`
- [x] T015 Add the Patrol panel to the operator console, in
      `osprey/web/components/sentry.js`

**Checkpoint**: The feature is reachable from the console, the API and MQTT.

## Phase 5: Verification and documentation

- [x] T016 Test the manager: independent tours per camera, yielding, refusing,
      enable, disable, remove, in `osprey/tests/test_sentry_manager.py`
- [x] T017 Test the wiring: the `PATROL` source, the recorded autotracking
      activity, the strict preset move and the config validator, in
      `osprey/tests/test_sentry_wiring.py`
- [x] T018 Test the API routes over real loopback HTTP, in
      `osprey/tests/test_webapi_sentry.py`
- [x] T019 Extend the runtime tests for the per-camera gate and the speed, in
      `osprey/tests/test_sentry_runtime.py`
- [x] T020 Write `docs/sentry.md`, including the Frigate zone caveat, and
      index it from `docs/README.md` and `docs/mqtt.md`
- [x] T021 Run the full gate: tests, mypy, ruff, repository policy and the
      Home Assistant static checks

## Phase 6: Deployment

**Purpose**: Prove the feature against the real camera and the real Frigate.

- [x] T022 Point the live Frigate autotracking at the full-frame
      `ptz_viewport` zone, so a detection outside the bottom of the frame still
      engages it while the camera is on patrol
- [x] T023 Add `car` to the live autotracking track list: the ptz camera
      detects cars and birds, and the old list was people and dogs only
- [ ] T024 Upgrade the live controller to this revision
- [ ] T025 Enable a patrol and confirm the sweep, the stand-down for
      autotracking, and the resume
