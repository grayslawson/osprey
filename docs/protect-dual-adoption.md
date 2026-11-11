# UniFi Protect and Osprey: custody versus re-sharing

This note explains what can and cannot be shared when a G5 PTZ is controlled
by Osprey. It is intentionally conservative and does not require a production
camera or controller for validation.

## Native adoption is exclusive

The physical G5 has one native UniFi controller relationship. Its private TLS
WebSocket (`secure_transfer`), PTZ channel (`ptz1`), settings acknowledgements,
and camera-originated media belong to that controller session. A second native
controller cannot subscribe to the same session.

Therefore a physical G5 cannot be natively adopted by Osprey and Protect at
the same time. Releasing, unmanaging, resetting, or re-adopting a camera is an
operational action outside routine software validation.

## Media-only re-sharing

Osprey can remain the sole native controller and re-publish the media it
already receives as standard RTSP and ONVIF. Frigate uses the normal Osprey
ONVIF endpoint (port `8000` by default) for PTZ and the RTSP endpoint (port
`8554`) for video. This does not create a second G5 controller and does not
give another consumer native custody of the physical camera.

Protect may be able to adopt a separate, generic ONVIF camera backed by this
re-published media. Protect's exact firmware and profile requirements vary, so
this remains an interoperability experiment rather than a compatibility claim.

## Read-only ONVIF persona

The optional read-only ONVIF persona serves the same ingested media while
omitting PTZ from capabilities, services, and media profiles. It returns a
standards-compliant fault for every PTZ read/write. Enable it on a separate
port, for example:

```json
{
  "host": "192.0.2.10",
  "ports": {"onvif": 8000, "onvif_read_only": 8001, "rtsp": 8554}
}
```

Add the Osprey host and port `8001` as a **generic ONVIF camera** in Protect;
Protect is adopting Osprey's media-only persona, not the physical G5. Use the
normal `8000` endpoint in Frigate so Frigate retains PTZ control. WS-Discovery
advertises the primary endpoint only, so enter the read-only host/port
manually if Protect does not offer a port field. Protect may reject the persona
or require additional profiles. Until tested on the exact Protect release,
firewall the read-only ONVIF and shared RTSP ports to the Protect host and
never expose them outside the trusted LAN.

## Safe validation

Run the fake-camera stack tests before trying a real NVR:

```sh
./osprey/test.sh
```

For a disposable client, verify that the normal endpoint exposes PTZ and the
read-only endpoint exposes media but faults on PTZ calls. Do not run custody,
reset, unmanage, or re-adoption commands against a production camera as part
of this compatibility test.

## Evidence in this repository

- [`pyunifiwire/SPEC.md`](../pyunifiwire/SPEC.md) records the measured camera
  control and media channels.
- [`osprey/ARCHITECTURE.md`](../osprey/ARCHITECTURE.md) documents the custody
  and northbound ONVIF/RTSP boundary.
- [`osprey/tests/test_stack.py`](../osprey/tests/test_stack.py) exercises the
  fake-camera session and ONVIF/RTSP faces without hardware.

## Running Frigate and Protect together

Osprey can serve both clients at once. Each one gets its own principal in the
users file (see [authentication](authentication.md#multiple-onvif-principals)):

| Client | Endpoint | Roles | What it gets |
|---|---|---|---|
| Frigate | `:8000` ONVIF, `:8554` RTSP | `media`, `events`, `ptz` | Video, events, autotracking PTZ |
| UniFi Protect | `:8000` ONVIF, `:8554` RTSP | `media`, `events` | Live view, recording, motion events |
| Operator | `:8000` ONVIF | `admin` | Everything, including encoder writes |

By default Protect gets no PTZ: its `protect` principal is denied on every PTZ
call with `ter:NotAuthorized`, so Frigate keeps sole control of the head.

### Letting Protect move the camera (manual wins, then resume)

Protect 7.3 and later can drive PTZ on third-party ONVIF cameras. To allow it,
add `ptz` to the `protect` principal's roles. Osprey then arbitrates:

- ONVIF users listed in `ptz.autotrack_users` (default `["frigate"]`) and
  anonymous ONVIF callers are autotrackers. Every other PTZ client is a person:
  other ONVIF users such as `protect`, the operator console, and MQTT commands.
- A person's command replaces an autotracker move that is still running and
  starts a hold of `ptz.manual_hold_sec` seconds (default 30). Each further
  manual command restarts the hold.
- During the hold, autotracker moves and Stops are answered with success but
  not sent to the camera, so Frigate does not log errors. When the hold ends,
  Frigate's autotracking takes over again.
- Set `ptz.manual_hold_sec` to `0` to turn arbitration off.

```json
{"ptz": {"manual_hold_sec": 30, "autotrack_users": ["frigate"]}}
```

This only works when Frigate and Protect use different ONVIF users. If ONVIF
authentication is off, every ONVIF caller counts as an autotracker.

### Seeing what an ONVIF client sends

Set `"onvif": {"log_calls": true}` to log every SOAP call at INFO with its
action, user, source address and PTZ source (`auto` or `manual`). Without it the
same line is logged at DEBUG. Use it while adopting a new client; it is noisy
while Frigate is tracking.

Adoption steps (Protect 7.2 or later; pin the console version and re-test after
each Protect update):

1. Create the `frigate` and `protect` users, with a separate password for each,
   and set `"allow_from"` on `protect` to the Protect console's address.
2. In Protect, use **Settings → System → Discover Third-Party Cameras**. If
   Osprey is on another subnet, use **Advanced Adoption** and enter the Osprey
   host and port `8000` by IP, because WS-Discovery is link-local multicast.
3. Enter the `protect` credentials. Choose the H.264 stream (`video1` for HQ, or
   `video2` if bandwidth is limited). Protect pulls one profile, and changing it
   later needs a remove and re-add.
4. Confirm live view, a recording, and a motion event before relying on it.
   Frigate should keep the same frame rate and detect latency it had before.

Keep the clock on Osprey and the console correct. WS-Security rejects a
timestamp more than 300 seconds from the server's clock.

### Keeping the physical G5 out of Protect

The G5 stays on the main LAN, so there is no camera VLAN. Use these controls
instead, and verify each one:

- **Firewall the physical camera away from the console.** Block the camera's
  traffic to the Protect console on TCP `7442`, `7444`, `7550` and UDP `10001`.
  `osprey protect-guard --apply` creates this policy on a UniFi gateway. See
  [Keep UniFi Protect off the physical camera](protect-guard.md). This stops
  Protect from natively adopting the physical camera while Osprey is its only
  controller.
- **Check the identity before adopting.** Osprey reports its model as
  `Osprey <camera model>`, a serial that starts with `OSP`, and a stable,
  locally administered MAC. Refuse any Adopt prompt whose MAC or serial is not
  one of Osprey's, or that is not an ONVIF device on the Osprey host and port.
- **Osprey never reports the G5's MAC or serial.** `GetNetworkInterfaces` and
  the device serial come from Osprey's own identity, so Protect cannot merge
  the two records. Set `onvif.identity.mac` explicitly if the derived MAC clashes
  with another device on the LAN.
- **If the physical camera is adopted by mistake,** Osprey loses its controller
  session. Stop the Protect adoption from the console, then restore Osprey's
  control from the Osprey admin console. Do this with a plan, because the
  step changes custody of the production camera.
