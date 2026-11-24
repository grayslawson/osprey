# Tasks: Osprey Assist

## Phase 0: core hooks

- [x] T001 `ASSIST` arbitration kind, `Controller.auto_age`, refusal instead of silent drop
- [x] T002 `scene` in `ptz` state, `assist_signals` in `/state`, scene on MQTT
- [x] T003 `assist_scope`: route scopes, ISP allowlist, token file
- [x] T004 Bearer principal in `webapi`, movement lease, `assist` config block
- [x] T005 Patrol hold and dwell override with expiry
- [x] T006 Console proxy to Assist
- [x] T007 Night view menu in the console patrol tab
- [x] T008 Align the movement lease docs with the code

## Phase 1: `osprey assist`

- [x] T101 Pillow metrics, synthetic scenes and monotonicity tests
- [x] T102 Baselines per view, day or night and hour; glare, blur, exposure and noise rules with the two-sample rule and hysteresis
- [x] T103 SQLite store, frames, retention (14 days quality, 3 days follow-up) and purge
- [x] T104 Providers (chat and decisions), per-task modes (rules, model, both), validation, budgets, stub model server
- [x] T105 Levers, ladders, per-view day and night ISP profiles
- [x] T106 Engine: gate, budgets, trials, night view search, apply, verify, revert, learning
- [x] T107 Assist HTTP API, console proxy path and the Assist tab
- [x] T108 Home Assistant discovery, MQTT approvals and ntfy
- [x] T109 `osprey assist` run, eval, replay and purge; launcher, entrypoint, Compose and Pillow in the image
- [x] T110 Closed-loop tests against a simulated camera behind the real web API
- [ ] T111 Acceptance run on the real camera in observe, then suggest (owner)
- [ ] T112 Home Assistant add-on option and NixOS service
