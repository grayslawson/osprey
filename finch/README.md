# finch — a UniFi camera that isn't there

**finch pretends to be a UVC G5 PTZ.** Point it at a UniFi Protect controller and
the controller discovers it, adopts it, configures it, records the H.265 video it
pushes, and pans and tilts it — with no camera involved anywhere.

It exists to make the camera side of the UniFi protocol testable. Working on a
controller, an NVR integration, or anything that has to talk to UniFi cameras
otherwise means owning one, wiring it up, and hoping it is in the state you need.
finch is a camera you can start, script, break and restart in a second.

> **This is not a way to make a third-party camera work with Protect.** For that,
> use [unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy), which is
> mature and does exactly that. finch is a *synthetic* camera, for testing and
> protocol work.

## What works

Verified against a real UniFi Protect **7.1.77**:

| | |
|---|---|
| Adopted, `state: CONNECTED` | ✅ |
| Full settings suite answered — 21 verbs | ✅ |
| Channels reported, enabled, streams armed | ✅ |
| **H.265 pushed, and recorded by the controller** | ✅ |
| **PTZ** — `ptz1` channel, preset config/go, motor broadcasts | ✅ |
| Snapshot upload on `GetRequest` | written, not yet exercised |
| Detection events, talkback, MJPEG | not built |

PTZ end to end with no hardware: an ONVIF `AbsoluteMove` arrives at
`cuckoo` — the other half of this project, a controller that isn't
there — becomes a scratch preset and a `go` on the `ptz1` channel, finch's virtual
head travels for a few seconds broadcasting `EventMotorState`, and the position
read back afterwards matches what was asked for.

## Requirements

- Python **3.13**. No third-party packages: finch itself is standard library only.
- `ffmpeg`, if you want finch to generate its own test video or snapshot stills.
- **[`pyunifiwire`](https://github.com/rjmotion/pyunifiwire)** — the wire itself: the message envelope, the
  WebSocket framing, the `extendedFlv` container, the HEVC bitstream. Not yet on
  PyPI:

  ```sh
  pip install git+https://github.com/rjmotion/pyunifiwire
  # or, if you are working on both at once, put it on the path instead:
  git clone https://github.com/rjmotion/pyunifiwire ../pyunifiwire     # run.sh and test.sh look for it there
  ```

## Run it

```sh
./test.sh                     # mypy --strict, then pytest. Needs no network at all.

# against a real controller — finch mints its own adoption token
./run.sh --host <controller> --user <user> --password <password> --source clip.h265

# against cuckoo — no token needed, cuckoo does not check one
./run.sh --host 127.0.0.1 --port 7442 --token anything --source clip.h265
```

Any Annex B HEVC file works as a source:

```sh
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=15 -t 4 -c:v libx265 \
       -x265-params keyint=15 -f hevc clip.h265
```

Useful flags: `--dump messages.jsonl` records every message the controller sends,
one JSON object per line — most of what is documented below was found that way.
`--mac` sets the hardware address, `--name` the name, `--once` disables
reconnection.

### Both halves at once

finch has a counterpart, `cuckoo` — a controller that isn't there, which adopts
real cameras and re-serves them as ONVIF. Run the two against each other in
containers and the whole protocol is exercised on any machine with no UniFi
hardware at all: finch dials cuckoo, is adopted, and streams; cuckoo re-serves the
result as RTSP, and `ffprobe` reports `hevc 1280x720 15/1`.

cuckoo is not published yet. Everything in this repository works without it — the
tests that read finch's output through cuckoo's ingest skip themselves when it is
not there.

## Before you point it at a real controller

**It adopts as a real device.** The controller will show a new camera, record from
it, and count it against whatever limits apply. finch calls itself `finch virtual`
and, given credentials, renames itself through the controller's API after
adoption — Protect names adopted cameras after their *model*, so a virtual G5 PTZ
otherwise appears under the same name as a real one sitting beside it. Delete it
from the controller when you are done.

**The default MAC is fixed.** It uses a Ubiquiti OUI so the controller finds it
plausible, with an invented tail. Two people running finch on one network would
collide — pass `--mac` to move it.

## Layout

Flat sibling modules, no package, no `__init__.py`. Everything is typed and
checked under `mypy --strict`.

| Module | What it is |
|---|---|
| `identity.py` | What finch claims to be, read off a real device |
| `adopt.py` | Minting an adoption token, and naming ourselves, via the controller API |
| `camera.py` | The state machine — answers everything the controller sends |
| `replies.py` | The payloads it answers with |
| `push.py` | Writing `extendedFlv` at the destination we are given |
| `gimbal.py` | A virtual head: presets, travel, arrival |
| `ptzchannel.py` | The second socket, on the `ptz1` subprotocol |
| `upload.py` | POSTing a snapshot to a one-time URL |
| `main.py` | Assembly and the run loop |

Everything to do with the wire — framing, envelopes, the container, the bitstream —
is in `pyunifiwire`, shared with the controller half. Where possible the tests run
finch's output through that other implementation's input: written from the same
measurements but by different code, so making one read the other is the strongest
check available without hardware.

## Things that cost us time

Written down because none of it is documented elsewhere, and each one presents as
a mystery rather than an error.

- **`camera-model` is the hex system id** (`0xa59b`), not `UVC G5 PTZ`. Send the
  model name and the controller's front end proxies your handshake upstream, gets
  a `400`, and drops you — immediately after the upgrade appeared to succeed.
- **Nulls in `ChangeVideoSettings` are questions, not values.** The controller asks
  with `"fps": null`, meaning *what are you set to?*. Echo the null back and it
  stores a channel with no frame rate and no bitrate, concludes there is nothing
  to stream, and never asks for video again. Report real values with
  `enabled: true` and it arms the stream immediately.
- **`onMetaData` is an AMF0 object (`0x03`), not an ECMA array**, carrying exactly
  nine keys — none of ffmpeg's usual ones. `channelId` is how the receiver files
  the stream (recordings are `<MAC>_<channelId>`), so a stream without it is a
  stream nothing is waiting for.
- **The push socket is not one-way.** The media server sends short TLV control
  messages back down it. finch logs them; they are not yet decoded.
- **A PTZ callback URL must carry its port.** cuckoo omitted it, so finch dialled
  `:7442` — which on a host already running a controller is somebody else's socket.

The full write-up, with captures, is in the project's
[protocol notes](https://github.com/rjmotion/unifi-guides/blob/main/real-controller-observations.md) §14.

## Credit

- [keshavdv/unifi-cam-proxy](https://github.com/keshavdv/unifi-cam-proxy) (MIT) —
  proved the token adoption path, and its `clock_sync.py` is the clearest
  description anywhere of the container's 16-byte inter-tag trailer.
- [NorthernMan54/unifi-cam-proxy-redalert](https://github.com/NorthernMan54/unifi-cam-proxy-redalert)
  (MIT) — documents the native discovery-and-adopt path and the camera model
  identifiers.

No code was taken from either. Both were read closely, and both saved us days.

## Licence

MIT — see [`LICENSE`](LICENSE).

Not affiliated with or endorsed by Ubiquiti Inc. UniFi and UniFi Protect are their
trademarks. No Ubiquiti firmware or binaries are included here.
