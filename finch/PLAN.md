# finch — a G5 PTZ that isn't there

A daemon that presents itself to UniFi Protect as a **UVC G5 PTZ**: discovered,
adopted, streaming, movable. The mirror image of `cuckoo`, which
pretends to be the controller.

Together they close the loop. Each one is checked against a real implementation of
the other, and then against each other with no hardware at all:

| Test | Proves |
|---|---|
| finch → **emu** (real Protect 7.1.77) | finch's camera impersonation is right |
| cuckoo → **real G5** | cuckoo's controller impersonation is right |
| finch → cuckoo, both in Docker | a regression loop needing neither camera nor controller |

The third row is the prize: a full protocol exercise that runs anywhere, on any
branch, without touching hardware.

---

## Prior art

Two projects already pretend to be a UniFi camera. Both were read before writing
this; neither is a base we can adopt wholesale, and both taught us something.

### keshavdv/unifi-cam-proxy — [keshavdv/unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy)

The established one. Wraps a real RTSP camera and presents it to Protect.

**What it proves works:** the token adoption path — dial
`wss://<nvr>:7442/camera/1.0/ws?token=<token>` with a **client certificate**, a
`camera-mac` header and subprotocol `secure_transfer`, then send
`ubnt_avclient_hello` carrying `adoptionCode`. Its settings responses (ISP, OSD,
video, device, sound/LED) are a working reference for payloads a controller
accepts.

**What it decodes for us:** `unifi/clock_sync.py` is the clearest description
anywhere of the container we call `extendedFlv`. Read against our own capture, the
16-byte trailer after each tag's previous-size field is:

```
00                     one zero byte
01 5F 90               0x015F90 = 90000  (video tags)   — the clock rate
00 2B 11               0x002B11 = 11025  (other tags)
00 × 8                 padding
uint32                 elapsed seconds × 100000
```

It also forces the FLV flags byte to `0x07` — which we measured independently —
and injects `onClockSync` and `onMpma` script tags every five seconds. We had the
trailer's *length* right and its *contents* only as "wall clock". Now we have both.

**What it does not do:** H.265, PTZ, or anything G5-specific. It is H.264 out of
ffmpeg into `nc`.

### NorthernMan54/unifi-cam-proxy-redalert — [NorthernMan54/unifi-cam-proxy-redalert](https://github.com/NorthernMan54/unifi-cam-proxy-redalert)

A rewrite, aimed squarely at newer Protect. Adoption stages 1–3 done, streaming
and PTZ not.

**What it gives us:** the *native* adoption path, which the token flow shortcuts —
Protect broadcasts on **UDP 10001**, the camera answers with a TLV identity block,
the operator clicks Adopt, Protect **POSTs `/api/1.2/manage` to the camera on
HTTPS 443** carrying the token and controller hosts, and only then does the camera
dial 7442. Its `camera_models.py` also carries the G5 PTZ identifiers
(`UVC_G5_PTZ` → platform `sav530q`, model id `0xa59b`).

**Where it disagrees with our measurements:** it describes `:7550` as a WebSocket
carrying control frames prefixed `DE 19 16 75 50`. Our G5 pushes **plain TCP** to
`:7550`, as the destination string `tcp://<host>:7550?retryInterval=1` says. Treat
this as model- or version-dependent and follow what we measured on this camera.

### Verdict: a new program, not a fork

Both forks are H.264, neither has PTZ, and the parts we would keep are payload
shapes rather than structure. Meanwhile the wire modules finch needs already exist
and are tested in cuckoo: `envelope`, `ws`, `flv`, `hevc`, `ptz`, `model` — the
same wire, read from the other end — and `stubcam.py` already *writes* H.265
extendedFlv with the correct trailer.

So: **finch is new, reuses cuckoo's protocol modules, and borrows payload detail
from both forks and from our own guides.** Standard library only, `mypy --strict`,
flat sibling modules, same as cuckoo.

Sharing is by `PYTHONPATH`, not by packaging: `finch/test.sh`, `pytest.ini` and
`mypy.ini` point at `../cuckoo`. No `__init__.py`, no install step. The one cost is
that finch is not standalone-publishable without cuckoo beside it; noted, accepted,
revisit if finch is ever published on its own.

---

## What finch has to be

Not invented — read out of the real camera's own record in the controller
(`/proxy/protect/api/bootstrap`):

| Field | Value |
|---|---|
| `type` | `UVC G5 PTZ` |
| `platform` | `sav530q` |
| `hardwareRevision` | `17` |
| `firmwareVersion` | `5.3.95`, build `148b9a3.260612.645` |
| channels | `2688×1512@30` 10 Mbps · `1280×720@30` 2 Mbps · `640×360@30` 800 kbps, idr 5 |
| `videoCodecs` | `h264`, `h265`, `mjpg` |
| `audioCodecs` | `aac`, `opus` |
| `smartDetectTypes` | `person`, `vehicle`, `animal` |
| `smartDetectAudioTypes` | `alrmSmoke`, `alrmCmonx`, `alrmBabyCry`, `alrmSpeak` |
| pan | steps 500–35500 (step 9), degrees −175…175 |
| tilt | steps 8000–18000 (step 7), degrees −10…90 |
| zoom | steps 0–730 (step 2), ratio 2 |
| focus | steps 0–255 |
| `hasMic` | true · `hasSpeaker` false · `canOpticalZoom` false |
| featureFlags | 98 keys total |

**MAC:** a Ubiquiti OUI, but **not** the real camera's. Same prefix, different
tail, so Protect sees a plausible second device and the real G5 is untouched.

---

## Milestones

Each is independently useful and independently verifiable. The controller-facing
ones are checked with the Protect API, not by looking at the UI.

**F1 — identity and skeleton.** The camera model (identity, channels, feature
flags) as data; the WebSocket *client* (cuckoo's `ws.py` is server-side: finch
needs the client handshake and frame masking); certificate generation. Tests: no
network.

**F2 — adoption against emu.** Mint a token (`POST /api/auth/login` then
`GET /proxy/protect/api/cameras/manage-payload` → `mgmt.token`, already verified
working on this controller), dial `:7442`, send hello, answer the settings suite,
stay up. **Done when** the API reports a second camera, `state: CONNECTED`, with
our model and MAC.

**F3 — media.** On `ChangeVideoSettings`, push extendedFlv to the destination the
controller names, with the 16-byte trailer and the periodic `onClockSync`/`onMpma`
tags. Start H.264 (both forks prove Protect accepts it), then switch to **H.265**
as the real G5 sends. **Done when** the API reports the camera recording and a
snapshot from Protect's own endpoint decodes.

**F4 — snapshots.** Answer `GetRequest` by POSTing a JPEG to the one-time `:7444`
URL. **Done when** Protect serves that image back.

**F5 — PTZ.** Accept `EnablePtzControl`, open the second socket with subprotocol
`ptz1`, handle `Preset` config-then-go, answer `GetCurrentPosition`, and stream
`EventMotorState` while a virtual gimbal travels. **Done when** a PTZ move driven
through the Protect API changes the position finch reports.

**F6 — events.** Emit `EventAnalytics` and `EventSmartDetect`. **Done when** the
event appears in Protect's own event list.

**F7 — the pair, in Docker.** Point finch at cuckoo instead of emu; both in
containers on one bridge network. **Done when** `docker compose up` adopts finch
into cuckoo, cuckoo serves the resulting stream over RTSP, and an ffprobe against
that RTSP URL reports HEVC — all with no camera, no controller, no host ports.

---

## Testing

Same shape as cuckoo: layer tests that need nothing, then an assembled test that
runs the real thing against a fake peer, then acceptance against the real
controller.

- **Layer** — identity payloads, TLV encoding, client handshake and masking, FLV
  writing (round-trip against cuckoo's `flv.Deframer`, which is the strongest
  possible check: the two halves must agree byte for byte).
- **Paired** — finch and cuckoo in one process, sockets on ephemeral ports: finch
  adopts into cuckoo, streams, moves. This is the regression test that replaces
  hardware.
- **Acceptance** — a script that drives the Protect API and asserts on state
  rather than screenshots: camera present, connected, recording, snapshot
  non-empty, PTZ position changed.

---

## Risks, and what we do about them

| Risk | Likelihood | What it costs | Mitigation |
|---|---|---|---|
| **Client certificate is fingerprinted** — Protect may want a real Ubiquiti camera cert | Low–medium | F2 blocked | unifi-cam-proxy uses a self-signed cert and works, so evidence is against it. If it bites, the failure is visible at handshake and we have firmware to extract from |
| **H.265 rejected from an adopted-by-token camera** | Medium | F3 partly blocked | Land H.264 first, then switch codecs. cuckoo's ingest already takes both, so F7 is unaffected either way |
| **Token flow changed in 7.1.77** | Low | F2 needs the native path instead | Token already retrieved from this controller. Fallback is redalert's discovery + `/api/1.2/manage`, which needs finch on its own IP (macvlan) — more setup, known to work |
| **MAC/OUI gating** | Low | adoption refused | Use a Ubiquiti OUI from the start |
| **Protect confused by a second G5 PTZ** | Low | noise in the UI | Different MAC and name; delete the device afterwards |
| **Recordings fill the disk** | Low | disk pressure | Short runs; 886 GB free; delete the device when done |
| **Scope** — F1–F7 is a lot of ground | Certain | time | Milestones are ordered so each is useful alone; stop anywhere |

**Deliberately not in scope:** talkback, firmware-update handling, MJPEG `:7551`,
multi-camera. And finch never touches the real camera — it does not need to.

### Evaluation

No blocking problem. The two real unknowns — the certificate check and whether
Protect will take H.265 from us — are both discovered cheaply, early, and neither
can damage anything. The adoption token, the identity fields and the container
format are all now *measured* rather than assumed, which is what the earlier work
was for. The main cost is time, and the milestone order means partial completion
still leaves something working.

Proceeding.

---

## Outcome

**F1–F3, F5 and F7 are done.** F1–F3 were verified against a real Protect 7.1.77
and F5 against cuckoo in Docker. F4 is written but not yet exercised; F6 is not
built.

**F5 held no surprises** — the PTZ protocol was already fully catalogued in
observations §13, so this was writing a virtual head, not discovering anything.
The one real bug was on the other side: cuckoo built its `EnablePtzControl` URL
without a port, so a camera on a non-default controller dialled `:7442` and
reached whatever else was listening there.

Both risks called out above resolved in our favour, and neither was where the
time went:

- **The certificate is not fingerprinted.** `ds` logged our self-signed cert's
  fingerprint and accepted the connection. That closes the guides' open question.
- **H.265 was accepted.** The media server switched to `VH265` and logged
  `RECORDING STARTED`. No existing camera-emulation project sends H.265.

What actually cost the time was three things nobody had documented, all now in
[observations §14](https://github.com/rjmotion/unifi-guides/blob/main/real-controller-observations.md):
`camera-model` must be the hex system id; nulls in a settings message are
questions rather than values; and `onMetaData` is an AMF0 object with a key set
that has nothing in common with ffmpeg's — including the `channelId` the receiver
files recordings under.

The pairing goal is met: `docker compose up` on the pair adopts
finch into cuckoo and serves the result as RTSP, with no camera and no controller
involved.
