# Cuckoo + Finch local Docker lab

This lab runs an entirely synthetic UniFi G5 PTZ control path on one laptop.
Finch simulates the camera around a selected local video, Cuckoo adopts it and
exposes RTSP/ONVIF, and Frigate 0.18 detects and autotracks the scene on the same
Compose network.

The lab now exercises the complete control path:

```text
local object fixture -> Finch virtual lens/HEVC -> Cuckoo RTSP -> Frigate detector
Frigate RelativeMove -> Cuckoo ONVIF/FOV conversion -> Finch PTZ channel
Finch EventMotorState -> Cuckoo MOVING/IDLE -> Frigate autotracker
```

It never contacts a physical camera, UniFi Protect, or another LAN service.

## Layout and pinned inputs

| Path | Upstream | Commit | Local branch |
| --- | --- | --- | --- |
| `cuckoo/` | `https://github.com/rjmotion/cuckoo` | `2e18666e9d9998b31d908e3a8f1d9ebb658c467f` | `dev/frigate-autotracking` |
| `finch/` | `https://github.com/rjmotion/finch` | `97c338adda3d6f9cfa35703c464826c37c12d63d` | `dev/frigate-autotracking` |
| `pyunifiwire/` | `https://github.com/rjmotion/pyunifiwire` | `ab424de6426826a9fba4cab7b5e20684355fea66` | `dev/frigate-autotracking` |

Each repository has one remote named `upstream`. The source changes remain
uncommitted on the local development branches.

The development image is based on Python 3.13 and includes FFmpeg, pytest,
mypy, `onvif-zeep`, and `uiprotect`. Frigate is pinned to this Linux amd64
manifest:

```text
ghcr.io/blakeblackshear/frigate:0.18.0-rc1@sha256:7060d1ec944e4276ae815a0025644de5ac52df978d061dc046876423c4ec5102
```

The checked-in fallback stream is deterministic raw Annex B HEVC, 1280x720 at
15 FPS:

```text
fixtures/frigate-person.h265
SHA-256 8DD45C792E439B174F9D2136737FF432A9339CEEAC379792EE3565E69681CB24
```

The original color-bar transport fixture is retained as
`fixtures/test-video.h265` with SHA-256
`D0EF3BB693D3E76E31CB347009BCF74C6893D8BB723A3560AA3D7766D9A674A5`.

## Network shape

```text
Finch synthetic camera
  -> cuckoo:7442  control and PTZ WebSocket upgrade
  -> cuckoo:7550  HEVC ingest

Frigate 0.18 (optional profile)
  -> cuckoo:8000  ONVIF
  -> cuckoo:8554/video2  RTSP/HEVC

Windows host, loopback only
  127.0.0.1:18000 -> Cuckoo ONVIF 8000
  127.0.0.1:18554 -> Cuckoo RTSP 8554
  127.0.0.1:15000 -> Frigate UI/API 5000
```

The project-scoped `cuckoo-lab_lab` bridge is not `internal: true` because
Docker Desktop Engine 29 suppresses published ports for internal networks.
All inbound ports bind explicitly to `127.0.0.1`; do not change them to
`0.0.0.0`. Cuckoo runs with `--no-announce`, and all destinations use Compose
DNS names rather than LAN addresses.

## Build and verify Cuckoo/Finch

From PowerShell in this directory:

```powershell
docker compose build cuckoo
docker compose up -d --wait --wait-timeout 120 cuckoo finch
docker compose --profile test run --rm integration-test
```

The bounded integration test verifies adoption, the `video2` ONVIF profile,
HEVC transport, PTZ `GetServiceCapabilities` with `MoveStatus=true`, the
`TranslationSpaceFov` advertisement, `MOVING -> IDLE`, the final virtual
position, and bidirectional Cuckoo/Finch PTZ traffic.

Finch and Cuckoo can each be restarted independently; the management setup
reissues `EnablePtzControl`, Finch reconnects the dedicated PTZ channel with
cancellable bounded backoff, and the integration test must still pass:

```powershell
docker compose restart finch
docker compose up -d --wait --wait-timeout 120 cuckoo finch
docker compose --profile test run --rm integration-test

docker compose restart cuckoo
docker compose up -d --wait --wait-timeout 120 cuckoo finch
docker compose --profile test run --rm integration-test
```

## Run the real Frigate tracking event

```powershell
docker compose stop frigate
docker compose --profile frigate run --rm frigate-init
docker compose --profile frigate up -d --force-recreate --no-deps `
  --wait --wait-timeout 180 frigate
docker compose --profile frigate run --rm --no-deps frigate-detection-test
docker compose --profile frigate run --rm --no-deps frigate-autotrack-test
docker compose --profile frigate run --rm --no-deps `
  frigate-visual-motion-test
```

`frigate-init` first moves the synthetic camera to motor/ONVIF center, waits
for both axes to report `IDLE`, and stores that position as `home`. Always rerun
it and then recreate Frigate after restarting Cuckoo; the preset is held in
Cuckoo process memory, and a running Frigate instance caches its preset list.
The autotrack verifier refuses to run when Frigate does not expose `home`.
The visual-motion verifier temporarily pauses Frigate autotracking, commands
positive FOV pan, positive FOV tilt, and positive zoom through Cuckoo, then
compares Finch's atomic viewport state with decoded Frigate frames. It requires
leftward content for the rightward viewport move, downward content for the
upward viewport move, and matching magnification for the narrower zoom
viewport; it returns home and reenables autotracking even on failure.

The detector test requires a new `person` event in `tracking_zone`, object data,
a score of at least 0.5, a persisted snapshot, and incoming camera frames. The
autotrack test starts from baseline event and traffic counters and requires all
of the following within 120 seconds:

- a new matching Frigate event;
- that exact event ID in Frigate's `New object` or `Reacquired object` log;
- a subsequent Frigate `called RelativeMove` log with a non-zero zoom value;
- at least one atomic Cuckoo configure/go preset pair whose motor zoom target
  differs from the pre-test target; and
- Finch `EventMotorState` responses after the baseline.

This correlation avoids passing on detection alone or on stale/manual PTZ
traffic. The Frigate UI is available at `http://127.0.0.1:15000` and is
unauthenticated only because the host binding is loopback-only.

The lab uses Frigate's experimental `zooming: relative` mode with
`zoom_factor: 0.75`. Relative mode sends pan, tilt, and zoom together in one
ONVIF `RelativeMove`, which makes the concurrent zoom path observable here.
Frigate documents `absolute` as more broadly compatible, but it issues zoom
separately and generally waits for a slow or stationary object. See
[Frigate camera autotracking](https://docs.frigate.video/configuration/autotracking/#zooming).

Inspect the negotiated feature set:

```powershell
Invoke-RestMethod http://127.0.0.1:15000/api/synthetic_g5_ptz/ptz/info
docker compose logs --no-color --tail 150 frigate cuckoo finch
```

Frigate should report `pt-r-fov` and must not emit its
`FOV relative movement not supported` disable diagnostic.

The lab currently uses a 0.40 per-frame minimum score and a 0.50 tracked-object
threshold for `person`. These are intentionally looser than Frigate's defaults
for the small, distant subject in the real G5 scene; lower values would invite
false positives from the tree line and roadside objects.

## Implemented ONVIF/PTZ behavior

- PTZ `GetServiceCapabilities` reports `MoveStatus=true` and
  `StatusPosition=true`; the device service keeps its distinct capability
  response.
- Known G5 PTZ identities advertise `TranslationSpaceFov`; generic cameras keep
  their existing relative space.
- FOV-relative requests are clamped to the ONVIF range, converted from half the
  current horizontal/vertical FOV to the G5 mechanical pan/tilt span, and then
  clamped to motor limits.
- Current FOV uses published wide/tele endpoints and linear interpolation over
  zoom. That interpolation is a testable approximation, not a claim about the
  physical lens curve; real-camera calibration may require a nonlinear map.
- Motion state is lock-protected and target-aware. Overlapping moves are
  rejected, stale terminal events cannot settle a newer move, disconnect is
  `UNKNOWN`, and the 60-second terminal-event timeout recovers to `UNKNOWN`
  instead of leaving the camera permanently busy.
- Configure-scratch-preset plus go-to-preset is serialized as one logical move,
  including request IDs and socket writes.

The geometry is based on Ubiquiti's published G5 PTZ values: 350-degree pan,
100-degree tilt, wide FOV H99.7/V51.9, tele FOV H45.5/V25.4, and 2x optical
zoom. Physical-camera testing and talkback remain out of scope for this lab.

## Run repository checks

The upstream `test.sh` files have CRLF line endings under the Windows Git
configuration, so run their contained commands without rewriting those files:

```powershell
docker compose run --rm --no-deps cuckoo bash -lc 'cd /workspace/cuckoo && export PYTHONPATH=/workspace/pyunifiwire/src && python3 -m mypy . && python3 -m pytest'

docker compose run --rm --no-deps finch bash -lc 'cd /workspace/finch && export PYTHONPATH=/workspace/pyunifiwire/src:/workspace/cuckoo && python3 -m mypy . && python3 -m pytest'

docker compose run --rm --no-deps cuckoo bash -lc 'cd /workspace/pyunifiwire && python3 -m mypy . && python3 -m pytest'
```

Verified results:

- Cuckoo: strict mypy clean; 228 tests passed.
- Finch: strict mypy clean; 80 tests passed.
- pyunifiwire: strict mypy clean; 53 tests passed.

## Regenerate the object fixture

The generated source and transparent foreground are rights-safe synthetic
assets:

```text
fixtures/frigate-person-source.png
fixtures/frigate-person-cutout.png
```

Regenerate the deterministic HEVC motion sequence from the cutout:

```powershell
.\docker\generate_person_fixture.ps1
Get-FileHash -Algorithm SHA256 .\fixtures\frigate-person.h265
docker compose up -d --force-recreate finch frigate
```

The script moves the person across the first eight seconds, then leaves twelve
empty seconds. That gap exceeds Frigate's ten-second return timeout so every
loop can return to the home preset before it closes/reacquires an object. The
real-event importer defaults to six blank seconds on each side, creating the
same twelve-second gap across its loop boundary. The private importer retains
the source resolution in an H.264 MP4 so Finch's virtual lens can crop a live
PTZ viewport. To use the non-object transport fixture without editing Compose:

```powershell
$env:FINCH_FIXTURE = '/fixtures/test-video.h265'
docker compose up -d --force-recreate finch
Remove-Item Env:FINCH_FIXTURE
```

### Import a private real G5 event

Normalize a user-owned recording for Finch's virtual lens. The importer retains
the source resolution, converts to a 15 FPS H.264 MP4, time-compresses the
event, and adds blank lead/tail frames so a loop creates distinct events:

```powershell
.\docker\import_real_event.ps1 `
  -InputPath 'C:\path\to\g5-event.mp4'

docker compose stop frigate
docker compose up -d --force-recreate finch
docker compose up -d --wait --wait-timeout 120 cuckoo finch
docker compose --profile frigate run --rm frigate-init
docker compose --profile frigate up -d `
  --force-recreate --no-deps --wait --wait-timeout 180 frigate
docker compose --profile frigate run --rm --no-deps `
  frigate-detection-test
docker compose --profile frigate run --rm --no-deps `
  frigate-autotrack-test
docker compose --profile frigate run --rm --no-deps `
  frigate-visual-motion-test
```

The importer writes `FINCH_FIXTURE` and `FINCH_VIRTUAL_LENS=1` to the ignored
`.env`, which Docker Compose loads automatically. The checked-in `.env.example`
selects the portable generated raw-HEVC fixture with the virtual lens disabled;
Compose uses those same values as its fallback when the variables are unset.
Do not put the selection in `.env.local`: Docker Compose does not load that file
automatically, which was the cause of the fixture reverting. The importer also
places explicit 10-minute and 30-second bounds on the encode and probe Docker
processes. The private `.env`, `.env.local`, and `fixtures/*.local.*` files
remain ignored. Do not add the source recording or generated private fixture to
version control.

The source image was produced with the built-in image-generation tool using
this exact prompt:

```text
Use case: photorealistic-natural
Asset type: deterministic local Frigate object-detection test fixture
Primary request: create a rights-safe synthetic surveillance-camera frame containing exactly one clearly visible full-body adult person walking across a residential driveway
Scene/backdrop: simple uncluttered suburban driveway and plain garage wall, no vehicles and no other people
Subject: one fictional adult person, head-to-toe fully visible, natural walking pose, occupying roughly 35 percent of image height, contrasting strongly with the background
Style/medium: realistic security-camera photograph, crisp but ordinary, no cinematic effects
Composition/framing: landscape 16:9, 1280x720 intent, person near the center with ample margin on all sides
Lighting/mood: even bright daylight, high visibility, minimal shadow
Constraints: fictional synthetic person; no recognizable public figure; no logos; no readable text; no watermark; no face close-up; no children; no weapons; no visual artifacts obscuring the body
Avoid: crowds, animals, cars, bicycles, signs, camera UI overlays, timestamps, motion blur, shallow depth of field
```

The transparent cutout edit used this exact prompt:

```text
Use case: background-extraction
Asset type: transparent foreground cutout for a deterministic computer-vision test video
Primary request: isolate the single adult person from the provided driveway image and remove everything else
Input images: Image 1 is the edit target; preserve the exact fictional adult person, clothing, walking pose, and full body
Composition/framing: full-body person centered with generous transparent padding; keep head, hands, legs, and shoes completely intact
Constraints: transparent background with a real alpha channel; change only the background; preserve the person exactly; no added shadow; no text; no watermark; no other objects or people
Avoid: opaque checkerboard, white backdrop, cropped limbs, altered clothing, altered face, extra anatomy, duplicated body parts
```

## Physical G5 PTZ handoff and one-time Frigate calibration

The physical path is opt-in and separate from Finch. It accepts only the
configured source IP and camera MAC before completing the camera WebSocket
upgrade. ONVIF, RTSP, and the Frigate UI remain loopback-only. On this Windows
host, Docker Desktop reserves but does not serve camera connections to its
LAN-IP-specific published ports under WSL mirrored networking. A small host-side
TCP relay binds only the configured LAN address and accepts only the configured
camera IP. Its upstream Docker ports 17442, 17444, and 17550 are loopback-only.

Current lab values live in ignored `.env.physical`; the checked-in example is
non-secret. Run the read-only preflight while the camera is still in Protect:

```powershell
.\docker\physical_preflight.ps1
```

Do not put camera or Protect passwords in `.env.physical`, Compose variables,
shell history, or chat. Protect **Unmanage** factory-resets the camera; current
Ubiquiti documentation lists the factory browser login as `ui` / `ui`. Use it
only in the local camera web page and set Protect Host IP to the verified
`CUCKOO_PHYSICAL_HOST`. The `/api/1.2/manage` helper is an undocumented fallback,
not the primary handoff path.

Stage the physical Cuckoo controller before Unmanage. Its loopback ONVIF/RTSP
ports are 18001/18555, and its eventual Frigate UI is 15001, so the synthetic
lab can remain available on 18000/18554/15000 during the handoff:

```powershell
docker compose --env-file .env.physical `
  -f compose.yaml -f compose.physical.yaml `
  -p cuckoo-physical-lab up -d --no-deps cuckoo
```

Start the LAN relay on Windows after Cuckoo is healthy. Keep it running while
the physical camera is in use. It is a raw TCP forwarder; it does not terminate
TLS or log camera payloads:

```powershell
python .\docker\physical_lan_proxy.py --env-file .env.physical
```

The relay logs its three listener addresses at startup. Stop it with Ctrl+C
when ending a foreground session. Do not start a second copy; a bind failure
means those ports are already occupied. Do not change the relay to bind
`0.0.0.0` or remove the camera-IP check.

After Unmanage, confirm the router still assigns `192.168.1.109` to the expected
MAC; the preflight port check is meant to run before the physical listener starts.
Do not start physical Frigate yet. Confirm physical Cuckoo logs show only the expected IP, MAC,
model, firmware, and both management/PTZ channels. Perform small signed motion
checks and choose a safe current home position. The physical initializer records
that current position without moving the camera:

```powershell
docker compose --env-file .env.physical `
  -f compose.yaml -f compose.physical.yaml `
  -p cuckoo-physical-lab --profile frigate run --rm --no-deps frigate-init
```

`frigate/config.physical.yml` tracks both `person` and `car`, uses relative zoom,
and has `calibrate_on_startup: true` for the first physical start. Frigate's
calibration intentionally moves through the PTZ range, takes about two minutes,
and can make its UI temporarily unresponsive. Start it only after direction and
clearance checks. The physical profile intentionally mounts this one config file
writable so Frigate can persist its generated `movement_weights`:

```powershell
docker compose --env-file .env.physical `
  -f compose.yaml -f compose.physical.yaml `
  -p cuckoo-physical-lab --profile frigate up -d --no-deps frigate
```

When calibration writes `movement_weights`, review the generated values in the
physical config and immediately set `calibrate_on_startup: false`. Otherwise a
restart repeats calibration and overwrites Frigate's refined weights. The
provisional inset `tracking_zone` must be replaced with the actual driveway/road
approach after inspecting the first physical frame; a full-frame required zone is
not recommended by Frigate.

No Windows Firewall rule is created by these files. Before handoff, add only
source-scoped inbound TCP rules for camera `192.168.1.109` to local ports 7442,
7444, and 7550, then remove those rules when returning the camera to Protect.

## Update the pinned upstream checkouts

Review changes before rebasing each local development branch:

```powershell
git -C .\cuckoo fetch upstream
git -C .\cuckoo log --oneline HEAD..upstream/main
git -C .\cuckoo rebase upstream/main

git -C .\finch fetch upstream
git -C .\finch log --oneline HEAD..upstream/main
git -C .\finch rebase upstream/main

git -C .\pyunifiwire fetch upstream
git -C .\pyunifiwire log --oneline HEAD..upstream/main
git -C .\pyunifiwire rebase upstream/main
```

Rebuild and rerun all tests after an update.

## Stop and remove only this lab

Preserve named state volumes:

```powershell
docker compose --profile frigate down --remove-orphans
```

Remove this Compose project's named volumes too:

```powershell
docker compose --profile frigate down --volumes --remove-orphans
```

The second command permanently removes only volumes declared by this project.
Images can be removed separately if no other project uses them.
