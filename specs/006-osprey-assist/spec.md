# Osprey Assist

Status: phase 0 shipped, phase 1 implemented and awaiting a hardware run.

## User outcome

A camera owner can switch on an optional helper that notices when the image
has gone bad (glare, blur, wrong exposure, noise), proposes a fix, and applies
it with the owner's approval, or without it at the autonomy level they choose.
The first target is the night glare on the G5 PTZ when infrared light bounces
off a nearby wall: Assist finds a nearby view that avoids it and proposes it as
the patrol's night view.

Assist is off by default and private by default. Nothing leaves the host unless
the owner opts a provider and a task into it.

## Scope

| Area | Decision |
|---|---|
| Where it runs | A separate process, `osprey assist`, from the same repository, image and release. The core stays standard library only. Pillow and paho-mqtt come with the `assist` extra. |
| First capability | Image quality: glare, blur, exposure, noise. Smart patrol, subject follow-up and self-tuning presets follow in later phases. |
| Model policy | Not rules-first by default. Each task is configured with rules, a model, or both, per task. Decision models are cheap and fast, so the model path is first class. Defaults come from a separate bake-off. |
| Providers | An OpenAI-compatible chat provider, and a decisions provider (state plus typed questions: `noul`, `choice`, `score`, `box`). |
| Approvals | Home Assistant over MQTT, and the console Assist tab. Optional ntfy. |
| Autonomy | Per capability: off, observe, suggest, act, auto. Full auto may change presets and the patrol structure inside budgets, with revert. Users can lower it. |
| Frames | Stored in Assist's own state directory. 14 days for quality frames, 3 days for follow-up frames, pinned exemplars kept. Sent to a model only when both the provider and the task allow it. |
| Follow-up (later) | Never while Frigate is tracking. Allowed after tracking ends if the subject stays and is small. Phase 1 leaves the hooks. |
| Cameras' levers | Pan, tilt, zoom and the ISP keys `hdr`, `gain`, `shutter`, `aeTargetPercent`, `brightness`, `contrast`, `sharpness`, `denoise`, `icrSensitivity`, `touchFocusX/Y`. The camera ignores `focusPosition`, `irLedLevel`, `irLedMode` and `wdr`, and nothing may send them. |

## Phase 0: core hooks

1. `ASSIST` arbitration kind that ranks with the patrol.
2. A bearer token principal, with a route scope and an ISP allowlist.
3. The day or night scene, and the live tracking and hold facts, in `/state`
   and on MQTT.
4. A patrol hold and a dwell override, both expiring and never persisted.
5. The console proxy to Assist.
6. The console can set a patrol point's night view.
7. `ARCHITECTURE.md` and the docs say which producers take the movement lease.

## Phase 1: `osprey assist`

Sampler, Pillow metrics, per-preset baselines, glare, blur and exposure
detection, per-preset day and night ISP profiles, the night view search for
glare, the decision log in SQLite with frames, the learning loop, approvals in
Home Assistant and the console, optional ntfy, guardrails and the kill switch,
and an `eval` and `replay` command for model bake-offs.

Out of scope for this phase: smart patrol, follow-up, self-tuning presets.
