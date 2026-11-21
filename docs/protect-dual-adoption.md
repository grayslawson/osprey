# UniFi Protect alongside Frigate

A UniFi camera has one native controller at a time. When Osprey is that
controller, Protect cannot natively adopt the same camera. Protect can still
record it, by adopting Osprey as a third-party ONVIF camera that serves the
media Osprey already receives.

There are two ways to set this up. Both leave the PTZ head with Frigate.

| | A: media persona (recommended) | B: one device, two users |
|---|---|---|
| What Protect adopts | A second ONVIF device on its own port | The primary device on port 8000 |
| How PTZ is withheld | The device has no PTZ at all | The `protect` user has no `ptz` role |
| Depends on ONVIF authentication | No | Yes: anonymous callers count as autotrackers |
| Extra setup | One port | An ONVIF users file |

Before either, block the physical camera from Protect with
[`osprey protect-guard`](protect-guard.md). Otherwise Protect lists the
camera with an **Adopt** button, and one click takes it from Osprey.

## A: the media persona

Set `ports.onvif_read_only` (or `--onvif-read-only-port`) to serve the camera
a second time with media and no PTZ:

```json
{
  "host": "192.0.2.10",
  "ports": {"onvif": 8000, "onvif_read_only": 8001, "rtsp": 8554}
}
```

| Device | Port | PTZ | For |
|---|---|---|---|
| Primary | 8000 | Yes | Frigate: video, events, autotracking |
| Media | 8001 | No | Protect: live view, recording, events |

The media persona has no PTZ service, no PTZ scope and no PTZ in its
profiles, and every PTZ call returns a SOAP fault. RTSP is shared on port
8554.

### Its own identity

The media persona is a separate device, so an NVR never merges it with the
primary. It has its own serial, MAC, hostname, endpoint UUID and friendly name,
`<camera name> (media)`. These are derived and do not inherit
`onvif.identity` overrides, which describe the primary device.

### Discovery

WS-Discovery answers a probe with one ProbeMatch per device. The media
persona is listed first, so a client that takes the first match gets the one
that cannot move the camera. A probe that asks for a PTZ device gets the
primary alone. The reply fits in one 1472-byte datagram, so it is not
fragmented on a 1500-byte MTU.

WS-Discovery is link-local multicast. It does not cross subnets or a container
bridge; in those cases add the device by address instead.

### Adopt it in Protect

1. In Protect, open **Settings → System → Discover Third-Party Cameras** and
   pick `<camera name> (media)`. Or use **Advanced Adoption** with the Osprey
   host and port 8001.
2. Enter the ONVIF credentials, if ONVIF authentication is on. Choose an H.264
   stream: `video1` for full resolution, `video2` for less bandwidth. Protect
   pulls one profile; changing it later means removing and re-adding the
   camera.
3. Check live view, a recording and a motion event. Frigate's frame rate and
   detect latency should not change.

Do not adopt the primary device in Protect. It carries PTZ, and Protect 7.3
and later can drive PTZ on third-party cameras.

Protect's third-party ONVIF support varies by release. Until you have tested
yours, firewall ports 8001 and 8554 so only the Protect console can reach them,
and keep them off untrusted networks.

## B: one device, two users

Give Frigate and Protect their own principals in the
[ONVIF users file](authentication.md#multiple-onvif-principals):

| Client | Roles | Gets |
|---|---|---|
| `frigate` | `media`, `events`, `ptz` | Video, events, autotracking |
| `protect` | `media`, `events` | Live view, recording, motion events |
| `admin` | `admin` | Everything, including encoder writes |

Every PTZ call from `protect` is refused with `ter:NotAuthorized`. Set
`allow_from` on `protect` to the Protect console's address.

To adopt, use **Discover Third-Party Cameras** or **Advanced Adoption** with
the Osprey host and port 8000, enter the `protect` credentials, and check as
in option A. Keep the clocks on Osprey and the console correct: WS-Security
rejects timestamps more than 300 seconds off (`OSPREY_ONVIF_MAX_SKEW_SECONDS`).

### Letting Protect move the camera

Add `ptz` to the `protect` user's roles. Osprey then arbitrates with "manual
wins, then resume":

- Users in `ptz.autotrack_users` (default `["frigate"]`) and anonymous ONVIF
  callers are autotrackers. Every other PTZ client counts as a person: other
  ONVIF users, the console and MQTT.
- A person's command replaces a running autotracker move and starts a hold of
  `ptz.manual_hold_sec` (default 30). Each manual command restarts the hold.
- During the hold, autotracker moves and stops are answered with success but
  not sent, so Frigate logs no errors. Autotracking resumes when the hold
  ends.
- `ptz.manual_hold_sec: 0` turns arbitration off.

```json
{"ptz": {"manual_hold_sec": 30, "autotrack_users": ["frigate"]}}
```

This needs ONVIF authentication on and separate users for Frigate and
Protect. Without authentication every caller is an autotracker.

## Tell the entries apart

Osprey reports its model as `Osprey <camera model>`, a serial starting with
`OSP`, and a stable, locally administered MAC. It never reports the camera's
own MAC or serial. Refuse any Adopt prompt whose MAC or serial is not
Osprey's, or that is not an ONVIF device on the Osprey host and port. If the
derived MAC clashes with another device, set `onvif.identity.mac`.

If Protect adopts the physical camera by mistake, Osprey loses its controller
session. Remove the camera from Protect, then point it back at Osprey.
Plan this, because it changes who controls a production camera.

## Debugging an ONVIF client

Set `"onvif": {"log_calls": true}` to log every SOAP call at INFO with its
action, user, source address and PTZ source (`auto` or `manual`). It is noisy
while Frigate tracks, so turn it off after adoption.

## Test without hardware

`make test` runs the fake-camera stack tests, which check that the primary
device serves PTZ and the media persona faults on it. The evidence is in
[`osprey/tests/test_media_persona.py`](../osprey/tests/test_media_persona.py),
[`osprey/tests/test_stack.py`](../osprey/tests/test_stack.py) and
[`specs/004-nvr-media-persona`](../specs/004-nvr-media-persona/spec.md).
