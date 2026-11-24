# Plan: Osprey Assist

## Boundary

The controller modules in `osprey/` stay standard library only. Assist is the
subpackage `osprey/assist/`. It imports nothing from the controller modules and
talks to the controller through `/api/v1` with its bearer token. It ships in the
same wheel and image, and `osprey assist` starts it.

## Decision structure

Every task (`quality.diagnose`, `quality.judge`) names a `mode`:

- `rules`: the local rules decide.
- `model`: the configured provider decides, and the rules only validate.
- `both`: both run; a disagreement is logged and the safer answer wins.

A provider is `chat` (OpenAI-compatible `/chat/completions`) or `decisions`
(`state` plus `questions`). Both are reached through LiteLLM in the homelab. A
reply that fails validation is logged and the task falls back to the rules.

## Safety order

Every change passes: schema, deny list, range and step, confidence, metric
consistency, cooldown and oscillation freeze, budget, gate (Frigate, a person,
the lease, the camera moving), then the autonomy level. The core enforces the
route scope, the ISP allowlist and the priority again.

## Verification

Unit tests per module, a stub model server, a fake camera with an optics
simulator for the closed loop, and a replay test over recorded fixtures. The
hardware acceptance run belongs to the owner.
