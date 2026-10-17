# The UniFi camera wire protocol

What a UniFi Protect camera and its controller actually say to each other. Every
value here was measured — from a real controller's own logs, from plaintext frames,
and from packet captures — against **UniFi Protect 7.1.77** and a **UVC G5 PTZ**
on firmware 5.3.95.

Where something is inferred rather than observed it says so. Where a widely held
belief is wrong, it says that too.

---

## 1. Shape of the thing

```
camera                                           controller
  │                                                   │
  │◄──── UDP broadcast :10001 ─────────────────────────   discovery probe
  ├───── UDP reply :10001 ─────────────────────────────►  "here is what I am"
  │                                                   │
  ├──── TLS WebSocket :7442 ──────────────────────────►   control channel
  │     subprotocol secure_transfer                       (JSON envelopes)
  │                                                   │
  ├──── TLS WebSocket :7442 ──────────────────────────►   PTZ channel
  │     subprotocol ptz1                                  (opened on request)
  │                                                   │
  ├──── plain TCP :7550 ──────────────────────────────►   media push
  │     extendedFlv, HEVC + AAC                           (camera dials out)
  │                                                   │
  └──── HTTPS POST :7444 ─────────────────────────────►   snapshot upload
        to a one-time URL the controller hands over
```

**After discovery, the camera dials every one of these.** The controller listens.
Discovery itself is the other way round: the controller probes, the camera answers.

---

## 1a. Discovery — UDP 10001

Ubiquiti's own discovery protocol, shared with access points and switches rather
than specific to cameras. The controller broadcasts a probe every few seconds, to
both `255.255.255.255:10001` and the multicast group `233.89.188.1:10001`:

```
01 00 00 00        version 1, command 0, payload length 0
```

Every device on the segment answers on the same port with the same header followed
by that many bytes of TLV — one-byte type, two-byte big-endian length, value:

```
01 00 00 aa   <type><len><value>  <type><len><value>  …
```

A real UVC G5 PTZ answers with eleven fields, in this order: `[MEASURED]`

| Type | Name | Length | Value |
|---|---|---|---|
| `0x02` | IPINFO | 10 | MAC + IPv4 packed together |
| `0x01` | HWADDR | 6 | the MAC again |
| `0x0a` | UPTIME | 4 | seconds |
| `0x0b` | HOSTNAME | text | `G5 PTZ` |
| `0x0c` | PLATFORM | text | `UVC G5 PTZ` |
| `0x17` | MGMT_IS_DEFAULT | 4 | `0` once adopted |
| `0x03` | FWVERSION | text | `UVC.SAV530q.v5.3.95.67.148b9a3.260612.0645` |
| `0x10` | SYSTEM_ID | 2 | `9b a5` — **little endian**, so model `0xa59b` |
| `0x20` | DEVICE_ID | text | a UUID |
| `0x2b` | GUID | 16 | a UUID, raw |
| `0x2c` | DEFAULT_CREDENTIALS | 1 | `3` |

⚠️ **`SYSTEM_ID` is little endian here** and big endian in the `camera-model`
WebSocket header — `9b a5` on this wire is `0xa59b` on that one. Read it the wrong
way round and you get a model that does not exist.

`DEVICE_ID` and `GUID` are the same values the camera later sends as its
`device-id` and `x-guid` headers, so a controller can tie a discovered device to
the connection it eventually makes.

Devices of every kind answer the same probe, so a reader must keep the field types
it recognises and step over the rest rather than failing.

**What happens next is not in this package.** The operator picks a device, and the
controller reaches the camera over HTTPS on port 443 with the adoption details —
`POST /api/1.2/manage` carrying a token and the controller's address. Only then
does the camera dial `:7442`. That request is documented by
[unifi-cam-proxy-redalert](https://github.com/NorthernMan54/unifi-cam-proxy-redalert)
and has **not been verified here**; a controller can also hand out an adoption
token through its own API and skip discovery entirely, which is the path used to
test everything below. `[UNVERIFIED]`

---

## 2. The control channel

### 2.1 The handshake

The camera opens a TLS WebSocket to `/camera/1.0/ws` on port 7442, presenting a
**client certificate** — self-signed is accepted; it is logged, not verified.

Headers, as the real camera sends them:

```
GET /camera/1.0/ws HTTP/1.1
Host: <controller>                 ← no port
Pragma: no-cache
Cache-Control: no-cache
Upgrade: websocket
Connection: close, Upgrade         ← not plain "Upgrade"
Sec-WebSocket-Key: <base64>
Sec-WebSocket-Version: 13
Sec-WebSocket-Protocol: secure_transfer
Origin: http://ws_camera_proto_secure_transfer
camera-mac: <MAC>                  ← no separators, upper case
camera-ip: <IP>
camera-model: 0xa59b               ← the hex system id, NOT the model name
camera-firmware: 5.3.95
device-id: <UUID>                  ← stable per device
x-guid: <UUID>                     ← per connection
adopted: true|false
```

⚠️ **`camera-model` is a hex id, not a name.** The controller's front end proxies
this handshake to an internal service and forwards the headers; a model *name* gets
a `400` from that service, and the camera sees a socket that closes immediately
after the upgrade appeared to succeed. This is the single most confusing failure in
the protocol. `[MEASURED]`

Before adoption the URL carries a token: `/camera/1.0/ws?token=<token>`.

### 2.2 The envelope

Every control message is UTF-8 JSON in one binary frame:

```json
{"from": "ubnt_avclient", "to": "UniFiVideo",
 "functionName": "ubnt_avclient_hello",
 "messageId": 10001, "inResponseTo": 0,
 "payload": { }, "responseExpected": false,
 "timeStamp": "2026-07-25T22:00:07.798+00:00"}
```

- `from`/`to` are `ubnt_avclient` (camera) and `UniFiVideo` (controller)
- a **reply echoes the request's `functionName`** and puts its id in `inResponseTo`
- ids are per-sender, so correlate on *name plus id*, never id alone
- frames may carry leading bytes before the `{`; scan for it rather than assuming
  offset zero `[MEASURED]`

### 2.3 Adoption

1. Camera sends `ubnt_avclient_hello` with its identity, its `features`, and
   `adoptionCode` set to the token.
2. Controller replies to that hello. **That reply is the gate** — until it arrives
   the camera retransmits and ignores everything else.
3. Controller sends `ubnt_avclient_paramAgreement`; the camera answers with the
   token under `authToken`.
4. Controller sends its settings suite, **one message at a time**, waiting for each
   acknowledgement. Sent as a burst, the camera stops acking and resets the channel.

The controller's hello reply is smaller than folklore suggests, and its contents
matter:

```json
{"protocolVersion": 67, "controllerName": "…", "controllerUuid": null,
 "controllerVersion": "7.1.77", "overrideUuid": true}
```

`controllerUuid` is **null** and `overrideUuid` is **true** on every reply. The
camera's persisted-UUID comparison is therefore a path the real controller never
takes — which is what lets a replacement controller adopt a camera that already
belongs to another one, without resetting it. `[MEASURED]`

### 2.4 Clock

The **camera** asks, not the controller: it sends `ubnt_avclient_timeSync` with
`{"timeDelta": 0}` while its envelope `timeStamp` carries its own clock. The reply
is **two keys**, both the controller's wall clock in milliseconds:

```json
{"t1": 1784981269413, "t2": 1784981269413}
```

No NTP is involved on this path. `[MEASURED]`

### 2.5 Nulls in settings messages are questions

`ChangeVideoSettings` arrives with `"fps": null`, `"bitRateVbrMax": null`,
`"enabled": false`. These are **not values to apply** — the controller is asking
*what are you set to?*.

A camera that echoes the nulls back has them stored verbatim, and the controller
then believes it has a camera whose channels have no frame rate, no bitrate and are
disabled. It never asks for video again, and nothing streams. Report your real
values, with `enabled: true`, and the controller arms the stream immediately.
`[MEASURED]`

---

## 3. Media

### 3.1 Arming

Streaming starts when the controller names a destination:

```json
{"video": {"video1": {"avSerializer": {
   "type": "extendedFlv",
   "parameters": {"streamName": "<16-char alias>", "withOpus": true,
                  "opusSampleRate": 24000},
   "destinations": ["tcp://<controller>:7550?retryInterval=1&connectTimeout=5"]}}}}
```

An **empty `destinations` list stops the track.** The camera dials the destination
itself and re-dials on failure. On the G5 PTZ all three channels are given the same
port; other models are reported to use one port per track, so do not assume.

### 3.2 The container

`extendedFlv` is FLV with two differences, and both matter:

```
"FLV" 01 07 00 00 00 09        header — flags byte is 0x07, not 0x05
00 00 00 00                    previous tag size

<tag header 11 bytes><body>    an ordinary FLV tag
00 00 XX XX                    previous tag size (4)
00 01 5F 90 00×8 XXXXXXXX      ← 16-BYTE TRAILER, not present in FLV
```

So there are **20 bytes between tags, not 4**. The trailer is:

| Bytes | Meaning |
|---|---|
| 1 | zero |
| 3 | `0x015F90` = 90000 on video tags — the clock rate; `0x002B11` = 11025 on everything else |
| 8 | zero padding |
| 4 | elapsed seconds × 100000, big-endian |

A reader that assumes plain FLV desynchronises on the second tag. `[MEASURED]`

### 3.3 The metadata tag, and why streams go missing

The first script tag is `onMetaData` — and it is an AMF0 **object (`0x03`)**, where
ffmpeg and most FLV writers emit an ECMA array (`0x08`). It carries exactly nine
keys, none of them ffmpeg's usual ones:

```
audioBandwidth = 64000     audioChannels = 1       audioFrequency = 16000
channelId      = 0         extendedFormat = true   hasAudio = true
hasVideo       = true      streamId = 1            streamName = "<alias>"
```

There is no `width`, `height`, `framerate`, `videocodecid` or `duration`; geometry
comes from the bitstream.

⚠️ **`channelId` is how the stream is identified.** Recordings are filed as
`<MAC>_<channelId>`, and until a stream announces the matching id the receiver logs
`NO INPUT STREAM <MAC>_0 FOR RECORDING AVAILABLE YET` and records nothing.
`streamName` is the per-session alias from `avSerializer.parameters`. On the G5 PTZ:
`video1` → channelId 0, streamId 1 · `video2` → 1, 2 · `video3` → 2, 4. `[MEASURED]`

Every few seconds the camera also sends `onClockSync` (`streamClock`,
`streamClockBase`, `wallClock`) and `onMpma` (a bitrate envelope).

### 3.4 Codecs

**Video is HEVC under FLV codec id 8** — an id no standard demuxer maps to HEVC.
The body is shaped like FLV's AVC packets: packet type, 24-bit composition time,
then length-prefixed NAL units, with the sequence header carrying an `hvcC` record
and arriving with `frameType 6`.

The camera advertises `videoCodecs: [h264, h265, mjpg]` and encodes whichever a
track is armed for, **including H.265 on one track and H.264 on another at the same
time** — confirmed against the real G5 PTZ. `[MEASURED]`

**The two codecs signal their configuration differently**, and this bites anyone who
assumes they match:

* **HEVC (codec id 8)** sends the parameter sets in a tag marked by **FLV frame type
  6**, and sets the `AVCPacketType` byte to **1** even on that config tag. The record
  is not a standard `hvcC` — the parameter sets are `[u16 length][NAL]` runs from
  byte 2. So config is told apart by the frame type, never the packet-type byte.
* **H.264 (codec id 7)** uses the **standard FLV convention**: a config tag is frame
  type **1** (a keyframe) with the `AVCPacketType` byte **0** and a normal `avcC`
  record at byte 5. Frames have `AVCPacketType` 1. There is no frame-type-6 tag.

Both then carry the picture as length-prefixed NAL units (one-byte NAL header for
H.264, two for HEVC). A parser must accept *either* signal — frame type 6 **or**
`AVCPacketType` 0 — or it will silently miss one codec's parameter sets and be
unable to describe the stream. `[MEASURED]`, both codecs, off a real camera and a
clean re-serve decode.

**Audio is AAC-LC, 16 kHz, mono**, regardless of the `withOpus` request in the
settings. A receiver that assumes 44.1 kHz plays it at the wrong speed. `[MEASURED]`

### 3.5 The push socket is not one-way

Once a stream is linked the receiver sends short TLV messages back down it — 2-byte
type, 2-byte length, body:

```
00 00 00 0c ff ff ff ff 00 00 00 00 00 ff 00 00    type 0, 12 bytes
00 02 00 03 00 01 01                               type 2, 3 bytes
00 02 00 03 80 00 01                               type 2, 3 bytes
00 01 00 00                                        type 1, empty
```

Framing is confirmed; the meanings are **not yet decoded**. A sender that never
reads its socket will not notice these, nor the receiver closing. `[UNVERIFIED]`

---

## 4. PTZ

PTZ is on this protocol, on a **second WebSocket**. After adoption the controller
sends:

```json
EnablePtzControl {"uri": "wss://<controller>/camera/1.0/ws"}
```

and the camera dials that URI negotiating subprotocol **`ptz1`**. `DisablePtzControl`
tears it down and carries no payload.

⚠️ If your controller is not on port 7442, **put the port in that URI** — otherwise
the camera dials 7442 and reaches whatever else is listening there.

### 4.1 There is no absolute-move verb

A move is *write a preset, then go to it*:

```json
Preset {"action":"config","items":[{"index":1,"name":"…",
        "pan":23502,"tilt":8000,"zoom":0,"focus":59}]}
Preset {"action":"go","index":1,"speed":1000,"notifyCommandStatus":{}}
```

`AbsolutePosition` is **never sent** by a real controller. Presets carry **focus**,
so the model is four axes, not three. `[MEASURED]`

### 4.2 Position, both ways at once

The controller polls `GetCurrentPosition {"inDegree":true,"inSteps":true}` *and*
consumes broadcasts — around 80 `EventMotorState` messages for a single move:

```json
{"ignoreActivity": true,
 "state": {"activity": 16, "focusMode": "manual", "scale": "normalized",
           "position": {"focus": 58, "pan": 23502, "tilt": 8000, "zoom": 0},
           "wallClockMs": 1784989188609}}
```

`activity` is a **flag word, not a magnitude** — 0 means settled. `scale` tells you
which coordinate system is in use and must not be assumed. The final broadcast's
position matches the requested preset exactly; that is the arrival signal.

Motor limits are announced by the camera in its hello `features` and **differ per
model** — read them, never hardcode them.

---

## 5. Snapshots

The controller does not fetch snapshots. It sends:

```json
GetRequest {"what": "snapshot",
            "uri": "https://<controller>:7444/internal/camera-upload/<token>",
            "timeoutMs": 60000, "quality": "medium"}
```

and the **camera POSTs the image** to that one-time URL. Any controller
implementation therefore has to serve port 7444. `[MEASURED]`

---

## 6. Confidence

| Tag | Meaning |
|---|---|
| `[MEASURED]` | Observed on the wire, in a capture, or in the controller's own logs |
| `[INFERRED]` | Derived from a working implementation or from adjacent measurements |
| `[UNVERIFIED]` | Framing seen, meaning unknown |

Anything not marked is `[MEASURED]`. The longer write-up, with captures and the
methods used to get them, lives with the
[flock guides](https://github.com/rjmotion/unifi-guides).
