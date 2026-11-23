# Osprey controller

This directory is the controller. It plays the part of a UniFi console for a
UniFi PTZ camera: it accepts the camera's connection, runs adoption and the
settings the camera expects, receives the video the camera pushes and moves
the head. It then serves all of that as standard RTSP, ONVIF and
WS-Discovery, for Frigate, Home Assistant, VLC or `ffplay`.

Every camera-facing behaviour was measured against a real controller and
camera. Osprey never uses SSH on the camera or patched firmware.

Users should start at the [project README](../README.md). This page is for
running from source and for the protocol facts that shape the code. The
layering is in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Verified on hardware

Osprey has driven a physical UVC G5 PTZ end to end: adoption, the settings
suite, PTZ, and all three encoder channels served as RTSP at once. `ffprobe`
reads H.264 Main at 2688×1512, 1280×720 and 640×360 with AAC, and an H.265
channel streams alongside H.264 ones. Home Assistant's ONVIF client
(`python-onvif-zeep`) has walked the full onboarding against both a fake peer
and the real camera; see [`harness/`](harness).

## Data flow

```text
   camera                          Osprey                        clients
  ────────                        ────────                      ─────────
  dials :7442  ──── control ────▶  adoption, settings, PTZ
  pushes video ──── :7550   ────▶  deframe, republish  ────────▶ RTSP :8554
  POSTs JPEG   ──── :7444   ────▶  snapshot store      ────────▶ HTTP snapshot
  Event*       ──── control ────▶  detections          ────────▶ ONVIF events
                                   device model        ────────▶ ONVIF :8000
                                                       ────────▶ WS-Discovery :3702
```

| Concern | Module |
|---|---|
| Device model, the seam between both sides | `model.py` |
| Adoption and settings suite | `adoption.py` |
| PTZ encoding, watchdog and presets | `ptz.py`, `presets.py` |
| Manual hold and autotracking arbitration | `arbitration.py` |
| Sentry patrol | `sentry.py`, `sentry_manager.py`, `sentry_runtime.py` |
| Detection events | `events.py` |
| Control-channel event loop | `controller.py` |
| Media ingest `:7550` | `media.py` |
| Snapshot upload `:7444` | `snapshots.py` |
| RTSP `:8554` | `rtsp.py` |
| ONVIF `:8000` and the media persona | `onvif.py`, `identity.py` |
| WS-Discovery `:3702` | `discovery.py` |
| Console API and setup wizard | `webapi.py`, `setup_wizard.py`, `web/` |
| MQTT bridge | `mqtt_bridge.py` |
| Taking custody of a camera | `manage.py`, `custody.py` |
| Assembly and flags | `main.py`, `config.py` |

Not implemented: talkback (AAC out), MJPEG on `:7551`, Imaging writes and a
wire-level preset delete.

The wire format (message envelope, WebSocket framing, the `extendedFlv`
container, the HEVC and AAC bitstreams) lives in
[`pyunifiwire`](../pyunifiwire/README.md).

## Run from source

Python 3.13 is required; the controller uses only the standard library.
`run.sh` and `test.sh` put the in-tree `../pyunifiwire/src` on the path.
FFmpeg is needed for the console preview and the media checks.

```sh
./test.sh                        # mypy, then pytest; no camera needed
./run.sh --host 192.0.2.10       # must be routable from the camera and clients
```

`--host` is written into the stream destinations, the snapshot upload URL,
the PTZ callback and every ONVIF address, so it cannot be a loopback. A
self-signed certificate is generated on first run (`osprey.pem`, mode `0600`,
ignored by Git).

## Configuration

Settings come from `osprey.json` (or `--config PATH`), flags, or both; a flag
always wins. The full list is in
[configuration](../docs/configuration.md).

```json
{
  "host": "192.0.2.10",
  "cameras": [
    {"mac": "AABBCCDDEEFF", "name": "Driveway", "ip": "192.0.2.40"},
    {"mac": "112233445566", "name": "Back yard"}
  ],
  "tracks": {"video1": "h264", "video2": "h265", "video3": "h264"},
  "ports": {"onvif": 8000, "rtsp": 8554}
}
```

```sh
./run.sh --config osprey.json                       # all from the file
./run.sh --config osprey.json --tracks video1:h265  # override one setting
```

`tracks` maps each of the three encoder channels (2688×1512, 1280×720,
640×360) to `h264` or `h265`. The default is H.264 on all three, because Home
Assistant's ONVIF integration needs an H.264 profile. A file that names one
channel changes only that channel. Codecs can also change at runtime through
ONVIF `SetVideoEncoderConfiguration`; Osprey re-arms the channel without a
restart.

`ports.onvif_read_only` adds the media persona, a second ONVIF device with no
PTZ. See [UniFi Protect alongside Frigate](../docs/protect-dual-adoption.md).

## Testing without a camera

`./test.sh` runs everything, with no hardware.

- Per layer: axis arithmetic, the settings suite, PTZ encoding, RTP
  fragmentation, SOAP parsing and discovery filtering.
- Adoption (`test_adoption_flow.py`) drives the real `Controller` against a
  fake camera, so the sequencing rules are executed rather than described.
- Whole stack (`test_stack.py`) starts every server on ephemeral ports, has a
  fake camera adopt over real TLS, and checks the ONVIF side: profiles, stream
  URI, a clamped PTZ move, an event reaching a pull-point subscriber and a
  snapshot round trip.

### Real video without a camera

`stubcam.py` pushes an HEVC or H.264 file at the ingest port the way a camera
would:

```sh
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=15 -t 4 -c:v libx265 \
       -x265-params keyint=15 -f hevc /tmp/t.h265
./run.sh --host 127.0.0.1 &
python3 stubcam.py /tmp/t.h265 --port 7550 --name video1 --loop
ffprobe -rtsp_transport tcp -i rtsp://127.0.0.1:8554/video1     # hevc 1280x720

ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=15 -t 4 -c:v libx264 \
       -x264-params keyint=15 -bsf:v h264_mp4toannexb -f h264 /tmp/t.h264
python3 stubcam.py /tmp/t.h264 --codec h264 --port 7550 --name video2 --loop
ffprobe -rtsp_transport tcp -i rtsp://127.0.0.1:8554/video2     # h264 1280x720
```

### A real ONVIF client

[`harness/`](harness) drives a running Osprey with `python-onvif-zeep`, the
client Home Assistant uses, validated against the ONVIF WSDL:

```sh
python3 harness/ha_walk.py  <host> <onvif-port> [--camera]   # Home Assistant's onboarding
python3 harness/ptz_walk.py <host> <onvif-port>              # every move kind and presets
```

## Testing with the real camera

An adopted camera reconnects to whoever holds `:7442`, but it refuses orders
from a controller it was not told to adopt, and only its current controller
can tell it. `handoff.sh` does the whole exchange:

```sh
./handoff.sh status        # who holds each port
./handoff.sh take          # release from the resident controller and point the camera here
./handoff.sh give-back     # stop Osprey and hand the camera back
```

`take` asks the resident controller to release the camera, frees `:7442`,
then POSTs `/api/1.2/manage` to the camera so it dials Osprey and adoption
starts. Credentials come from the directory in `OSPREY_SECRETS` (default
`~/.osprey/secrets`), which is never committed. The header of `handoff.sh`
lists every `OSPREY_*` override. For a factory-reset camera, `manage.sh` sends
the manage request directly.

## Protocol notes

- **The video is HEVC under FLV codec id 8**, which no stock demuxer maps, in
  a container with 20 bytes between tags where FLV has 4. The sequence header
  is not a standard `hvcC`: it length-prefixes the parameter sets and signals
  config by the FLV frame type.
- **The two codecs signal config differently.** HEVC uses FLV frame type 6
  (packet-type byte 1 even on config); H.264 uses frame type 1,
  `AVCPacketType` 0 and a normal `avcC`. Assuming they match silently loses
  one codec's parameter sets. Codecs are read from the wire, never assumed,
  and re-packetised without transcoding as RFC 7798 (HEVC) and RFC 6184
  (H.264) RTP.
- **Home Assistant needs an H.264 profile.** It rejects an H.265-only device
  with "no H.264 streams available" and does not reconfigure the encoder
  itself.
- **The audio is AAC-LC at 16 kHz mono**, so the SDP states a 16000 clock
  rate. A receiver assuming 44.1 kHz plays it at the wrong speed.
- **timeSync is a ping-pong.** The camera pings repeatedly, each message
  referencing the controller's last, until its clock converges, and only then
  sends hello. Answer every timeSync and never speak first.
- **There is no absolute-move verb.** A move configures a preset and then goes
  to it. Presets carry focus, so the model has four axes.
- **ONVIF `ContinuousMove` becomes a relative step**, because the camera has
  no continuous mode. A client holding an arrow key sends these repeatedly.
- **`RemovePreset` forgets the slot locally.** No wire delete has been
  observed, so the slot stays on the camera.
- **The preset table is Osprey's**, persisted to `osprey-presets.json`,
  because the camera cannot list its slots. It is restored on connect and
  written to the camera once per start; see
  [PTZ health](../docs/ptz-health.md).
- **ONVIF `Capabilities` order matters.** It is a strict schema sequence, and
  a real client drops out-of-order elements. Get it wrong and Home Assistant
  adds the camera with no event sensors.
- **WS-Discovery must use WS-Addressing 2004/08, not 2005/08.** ONVIF pairs
  the 2005/04 discovery draft with the 2004/08 addressing namespace. A device
  that uses 2005/08 finds no `MessageID` in the probe and never appears in
  Home Assistant's discovery, and a self-written probe will not catch it. See
  the `discovery.py` docstring and `tests/test_discovery.py`.
- **ONVIF authentication is on for new installs** and opt-in for existing
  ones. Test UsernameToken support before enabling it on a running install,
  and never expose an anonymous ONVIF port to an untrusted network. See
  [security](../docs/security.md#secure-mode-and-legacy-mode).

The full protocol write-up, with captures, is in the
[flock guides](https://github.com/rjmotion/unifi-guides).

## Licence

MIT; see [`LICENSE`](../LICENSE).

Not affiliated with or endorsed by Ubiquiti Inc. UniFi and UniFi Protect are
its trademarks. No Ubiquiti firmware or binaries are included.
