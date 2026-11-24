# Osprey Assist

Osprey Assist watches the camera's picture, spots glare, blur, bad exposure and
noise, and proposes a fix. You approve it in the console or in Home Assistant, or
you let Assist apply it within limits and undo it when it does not help. It is
optional, it is off until you switch it on, and it is a separate process, so the
controller does not depend on it.

It ships in the same image and wheel as Osprey. This release covers picture
quality. Smart patrol, follow-up on small subjects and self-tuning presets come
later, and their autonomy settings are accepted but do nothing yet.

## What it does

Assist looks at a snapshot when the patrol arrives at a view, every 10 minutes
while the camera is parked, when day turns to night, and when you press **Check
now**. For each view and for day and night separately it learns what normal looks
like, so it flags a view that is unusual for itself, not one that is merely dark.

| Problem | Typical first fix |
|---|---|
| Glare at night | Find a nearby view that avoids it, and suggest it as the patrol's night view |
| Glare by day | Lower `aeTargetPercent` one step, then `hdr` |
| Blur | A small zoom nudge, which makes the camera refocus |
| Too dark | Raise `aeTargetPercent`, then `brightness` |
| Too bright | Lower `aeTargetPercent`, then `contrast` |
| Noise | Raise `denoise` |

The night glare fix is the one it was built for. On a G5 PTZ the infrared light
can bounce off a wall near the camera and wash out the picture. The camera
ignores `irLedLevel`, so Assist looks for a view a few percent of the pan or tilt
range away where the glare drops by at least half, and proposes saving it as
`<Name>Night` and using it at night.

Assist never sends `focusPosition`, `irLedLevel`, `irLedMode` or `wdr`. The camera
accepts them and ignores them, and Osprey refuses them from Assist's token.

## Turn it on

1. In `osprey.json`, enable the door Assist uses. Osprey creates the token on the
   next start:

   ```json
   {"assist": {"enabled": true}}
   ```

2. Write `osprey-assist.json`:

   ```json
   {"enabled": true}
   ```

3. Run Assist from the same image. In Compose, uncomment the `assist` service in
   `compose.release.yaml`. Without a container:

   ```sh
   pip install "osprey-ptz[assist]"
   osprey assist run
   ```

With nothing else set, Assist uses rules only, asks before every change, and sends
nothing off your network. The console gets an **Assist** tab once Osprey can
reach it.

Assist listens on `127.0.0.1:8090`. Keep it there. Osprey's console talks to it,
and its token is the only thing that protects it.

## Autonomy

Each capability has one of five levels. Set it in the console, in Home Assistant,
or in the config.

| Level | What happens |
|---|---|
| `off` | Nothing runs. |
| `observe` | Assist looks and logs what it would do. It writes nothing to the camera. |
| `suggest` | Assist tries the fix, takes an after picture, puts everything back, and waits for you. This is the default. |
| `act` | Assist applies safe changes itself, checks them, and reverts a bad one. Changes to presets or the patrol wait for you. |
| `auto` | Like `act`, and it may also add a night view and set it on the patrol, inside its budgets and with a revert. Turn that part off with `guardrails.structural_in_auto: false`. |

Every applied change is checked straight away and again after a minute and after
ten, each time the camera is at that view. It is reverted if the problem is not at
least 10% better, if something else got more than 10% worse, or if a judge model
says it looks worse. A reverted lever rests for 24 hours, and an undo you press
rests it for seven days.

A new night view that is reverted is removed from the patrol, and the saved
preset is left in place, because the Assist token cannot delete presets. Delete it
from the Presets tab if you do not want it.

## What it will not do

- It does not move the camera while Frigate is tracking, while a person holds the
  camera, while another producer has the movement lease, or while the camera is
  moving. Osprey enforces the first two again on every move.
- It keeps to budgets per camera: 6 moves and 6 setting changes an hour, 4 trials
  an hour, 2 night searches per night and 2 preset changes a day. A night search
  tries at most 8 offsets, none more than 4% of the pan or tilt range from the
  view.
- It keeps at most one open suggestion or unverified change per view.
- A lever that flips direction twice in six hours is frozen, and Assist asks you
  instead.
- It never edits `sentry.json`. A night view goes through the same API call the
  console uses.
- `quiet_hours` (for example `["22:00-06:00"]`) stops moves. Setting changes
  continue unless you set `quiet_isp`.
- The kill switch (the console toggle, the Home Assistant switch or `enabled:
  false`) stops Assist, releases the patrol and writes back the settings it
  changed. To cut Assist off from outside, turn off `assist.enabled` in
  `osprey.json` and restart Osprey: the token stops working.

The camera has no read-back for image settings, so Assist keeps a record of what
it wrote and assumes a default for a setting it has never written. If you change a
setting in Protect or elsewhere, Assist does not see it. Day and night profiles
are written again each time the patrol arrives at a view.

## Approving

**Console.** The **Assist** tab shows each waiting suggestion with the picture
before and after, the exact change, what to expect and how sure Assist is. Press
**Apply**, **Reject** with a reason (not a problem, wrong fix, looks worse, do not
touch this view) or **Snooze** for a day. Suggestions expire after 72 hours.

**Home Assistant.** With an MQTT broker, Assist adds a device under
`osprey/<device>/assist/` with discovery. Entities: an on and off switch, a mode
select, pending approvals, last decision, before and after images, **Approve
newest** and **Reject newest** buttons, a `Suggestion` event for automations, and
model calls today. Selecting `auto` over MQTT is refused unless you set
`notify.allow_auto_from_mqtt`. A command can only apply a suggestion that already
exists and passed every check.

```json
{"notify": {"broker": {"host": "192.0.2.5", "username": "assist", "password_file": "/run/secrets/mqtt"}}}
```

**ntfy.** Set `notify.ntfy` with a `url` and `topic` for a phone message that
links to the console. Pictures attach only with `attach_frames: true`.

## Models

Each task picks how it decides, in `tasks`:

| Mode | Meaning |
|---|---|
| `rules` | Local rules only. The default until you add a provider. |
| `model` | The model decides. Rules still check its answer against the picture's numbers. |
| `both` | Both run. If they name different problems, `on_disagree` says whether the rules win, the model wins, or nothing happens (`log`, the default). |

The two tasks are `quality.diagnose` (what is wrong, and which fix) and
`quality.judge` (is the after picture better). A model answer that fails
validation is logged and the rules decide, so a bad reply cannot change the
camera.

A provider is one of two kinds:

- `chat`: an OpenAI compatible `/chat/completions` endpoint. It returns JSON that
  Assist validates.
- `decisions`: OpenRouter style decision models. Assist sends the context and
  typed questions (`choice`, `noul`, `score`) and reads back probabilities.
  The confidence of a choice is its probability.

```json
{
  "providers": {
    "gateway": {"kind": "decisions", "base_url": "http://litellm.lan:4000", "path": "/decisions",
                "api_key_file": "/run/secrets/assist-llm", "allow_frames": true}
  },
  "tasks": {
    "quality.diagnose": {"mode": "both", "provider": "gateway", "model": "liquid/d1.2", "send_frames": true},
    "quality.judge": {"mode": "model", "provider": "gateway", "model": "liquid/d1.2", "send_frames": true}
  }
}
```

A picture leaves the machine only when the provider has `allow_frames: true` and
the task has `send_frames: true`. A private address does not count: a gateway on
your LAN can forward to a cloud model. Without both, the model sees only numbers
and a map of where the bright areas are. Each provider has hourly and daily call
limits (`max_calls_per_hour`, `max_calls_per_day`), and past them Assist uses the
rules.

The decisions path is the one a gateway exposes. The request body follows
OpenRouter's alpha decisions API. It has been checked against a stub and against
how other code in this setup calls the API, not against every decision model.

### Compare models

`osprey assist eval` scores a task on frames you have labelled. Put the frames and
a `cases.json` in a folder:

```json
[{"id": "porch-1", "file": "porch-1.jpg", "scene": "NIGHT", "preset": "BackPorch", "label": "glare"},
 {"id": "yard-1",  "file": "yard-1.jpg",  "scene": "DAY",   "preset": "Yard",      "label": "ok"}]
```

```sh
osprey assist eval --cases ./cases --mode rules
osprey assist eval --cases ./cases --mode model --provider gateway --model liquid/d1.2 --send-frames --out run.json
```

Labels are `glare`, `blur`, `under`, `over`, `noise` or `ok`. For `quality.judge`
use `{"before": "a.jpg", "after": "b.jpg", "problem": "glare", "label": "after"}`
and `--task quality.judge`. The output is accuracy next to the rules baseline, the
share of replies Assist could use, latency (median and 95th percentile), cost and
tokens. `osprey assist replay` builds the cases from the suggestions you approved
or rejected.

## Data and privacy

Assist keeps its own state directory (`/var/lib/osprey-assist` in the container),
mode 0700 with files 0600. Osprey never reads it.

| Data | Kept |
|---|---|
| Quality and trial frames | 14 days |
| Follow-up frames | 3 days |
| Frames of a waiting or recently settled suggestion | until 7 days after it is settled |
| Metrics samples | 90 days |
| Decisions, outcomes and feedback | 365 days |
| Baselines, lever statistics and profiles | until you purge |

`storage.max_frames_mb` (default 2048) removes the oldest frames first. Change any
of these under `storage`.

`osprey assist purge --yes` or **Forget everything** in the console deletes the
frames, the log and what it learned. Your settings stay.

Prompts are not logged. Names of presets are sent to a model unless you set
`privacy.redact_names`. Keys come from a file or an environment variable and are
never logged.

## What was not verified on hardware

The ISP settings have no read-back and their ranges differ by model. The step
sizes and limits for `aeTargetPercent`, `brightness`, `contrast`, `sharpness`,
`denoise` and `hdr` are cautious defaults, and the assumed starting values are
guesses. `touch_focus`, `gain` and `shutter` are off until you give them limits
under `capabilities.quality.limits`. Run Assist in `observe`, then in `suggest`,
on your camera before you trust `act`.

## Reference

Every key in `osprey-assist.json`; unknown keys are an error.

| Key | Default | |
|---|---|---|
| `enabled` | `false` | Master switch. The console and Home Assistant can also change it. |
| `osprey.url`, `osprey.token_file`, `osprey.cameras` | `http://127.0.0.1:8000`, `/var/lib/osprey/assist-token`, `["default"]` | How Assist reaches Osprey. |
| `listen.bind`, `listen.port` | `127.0.0.1`, `8090` | Assist's own API. |
| `capabilities.quality.mode` | `suggest` | The autonomy level. |
| `capabilities.quality.problems` | all five | Which problems to look for. |
| `capabilities.quality.levers` | see below | Which levers it may use. |
| `capabilities.quality.limits` | `{}` | `{"lever": {"lo", "hi", "step", "default"}}` overrides. |
| `capabilities.quality.periodic_min` | `10` | Minutes between checks while parked. |
| `capabilities.quality.verify_after_sec` | `[60, 600]` | When to re-check an applied change. |
| `capabilities.quality.min_gain`, `max_regress` | `0.10`, `0.10` | Better by this much to keep, worse by this much to revert. |
| `capabilities.patrol`, `followup`, `presets` | `off` | Accepted, not implemented yet. |
| `guardrails.*` | see above | `moves_per_hour`, `isp_writes_per_hour`, `trials_per_hour`, `probe_sessions_per_night`, `max_probe_offsets`, `max_offset_frac`, `preset_changes_per_day`, `structural_in_auto`, `tracking_hold_sec`, `pending_expire_hours`, `quiet_hours`, `quiet_isp`. |
| `providers`, `tasks` | none, rules | See Models. |
| `notify.*` | MQTT on | `mqtt`, `mqtt_frames`, `ha_discovery`, `discovery_prefix`, `base_topic`, `device`, `broker`, `allow_auto_from_mqtt`, `ntfy`. |
| `storage.*` | see Data | |
| `privacy.*` | off | `redact_names`, `debug_prompts`. |

Default levers: `aeTargetPercent`, `brightness`, `contrast`, `sharpness`,
`denoise`, `hdr`, `zoom_nudge` and `night_view`.

Set `OSPREY_ASSIST_CONFIG` to use a config file somewhere else.

## Core hooks

Osprey itself provides what Assist needs, and you can use these without Assist.

- **A day or night signal.** `scene` is in the state document, in the `ptz` event and in
  the MQTT `state/ptz` message.
- **A lowest priority for Assist.** Its moves rank with the patrol's. A person
  holds it off, Frigate takes the camera from it, and it never starts a manual
  hold. A move that a person's hold dropped is refused, not reported as done.
- **A token for Assist.** It is a bearer token, not a console login. It can call
  a short list of routes, and for ISP it can change only a short list of
  settings. See the [API](api.md#osprey-assist-routes).
- **A patrol hold and dwell override.** They expire on their own and neither is
  saved. See [Sentry](sentry.md#pausing-and-reshaping-the-patrol-at-runtime).
- **A console door.** `/api/v1/assist/...` forwards the console's requests to
  Assist once you are signed in.
- **A night view menu** on each patrol position in the console.

The token file is created readable only by its user and is never changed by a
restart. Delete it and restart Osprey to rotate it. Set `assist.enabled` back to
`false` (and unset `OSPREY_ASSIST_TOKEN_FILE`) to close the door. `scopes` in the
`assist` block limits the token: `quality` covers reading, moving, going to
presets, ISP and the patrol hold, and `presets` adds saving presets and setting a
patrol point's night view. Both are on by default.
