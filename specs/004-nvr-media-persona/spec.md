# NVR media persona

Status: implemented.
Design notes: `specs/004-nvr-media-persona/`.

## User outcome

An operator runs Osprey for Frigate and also wants the camera to appear in
UniFi Protect — live view, recording, motion events — **at the same time**,
without Protect ever touching the camera head.

Osprey already serves the camera over ONVIF and RTSP. What is missing is a
way to hand Protect a *second, media-only* view of the same camera that
Protect can find, adopt and record from, while Frigate keeps the PTZ channel
to itself.

## Scope

| Area | Decision |
|---|---|
| How many devices | One physical camera, two ONVIF devices: the primary persona (`:8000`, media + PTZ) and the media persona (`:8001`, media only). |
| Who gets what | Frigate uses the primary persona for PTZ and RTSP. Protect adopts the media persona for live view, recording and events. |
| PTZ on the media persona | Structurally absent: no PTZ service, no PTZ scope, no PTZ in the media profiles, and every PTZ call faults. Protect cannot move the head even if it tries. |
| Identity | The media persona is a *different* device: its own serial, MAC, friendly name and endpoint UUID. An NVR records it as its own camera and never merges it with the physical G5 or with the primary persona. |
| Discovery | WS-Discovery answers with one ProbeMatch per persona, so Protect finds the media persona by discovery rather than by a hand-typed port. |
| Physical camera | Unchanged. It stays Osprey's, and `protect-guard` keeps the NVR off it. |
| Frigate | Unchanged. It keeps the primary persona, its autotracking and its RTSP. |

## Behaviour

### Two devices, one camera

The primary persona answers on the configured ONVIF port. The media persona
answers on `ports.onvif_read_only`. Both serve the same ingested media; the
media persona simply omits PTZ.

Both are advertised by WS-Discovery, each with its own endpoint UUID and its
own XAddr, so a client that discovers Osprey sees two ONVIF devices and can
tell them apart.

### A distinct identity

The media persona derives its serial, MAC and hostname from the camera seed
*and* a persona marker, so it never repeats the primary persona's identifiers
and never repeats the physical camera's. Its friendly name carries the persona
marker too, so it is obvious in the NVR's camera list which entry is the
media-only view.

A persona derives everything that identifies the device. It deliberately does
**not** inherit the primary's operator overrides (`onvif.identity.serial`,
`onvif.identity.mac`, `onvif.identity.hostname`) nor the MAC of the discovery
interface: those describe *one* device, and sharing them would make the two
personas indistinguishable to an NVR. The manufacturer, model and hardware id
describe the product, and are shared.

### The media surface

The media persona serves the same video profiles, stream URIs and snapshot
URIs as the primary, minus PTZ:

- `GetServices` and `GetCapabilities` omit the PTZ service.
- The media profiles carry no PTZ configuration.
- Every PTZ operation returns a standards-compliant fault.
- The advertised scopes carry no PTZ scope, so a client that only wants a
  media device is not offered a PTZ one.

### Discovery answers for every persona

A Probe that names scopes is answered by the personas that satisfy **every**
requested scope, so a client that asks for a PTZ device is offered the primary
persona alone and never the media-only one. A Probe that names no scope is
answered by all of them.

The media persona is listed **first**. A client that takes the first match
adopts the one that cannot move the camera, which is the safe default for the
recorder that discovers Osprey.

### One datagram

A 1500-byte Ethernet MTU leaves 1472 bytes for the UDP payload, and a larger
discovery message is split into IP fragments, which some networks drop and
some clients reassemble badly. Every message Osprey sends therefore stays
inside one datagram: the metadata version, the device type, the `To` header,
the XML declaration and finally the hardware scope are given up, in that
order, if the personas would not otherwise fit. A controller that serves one
camera still sends the full, standard message.

### Simultaneity

Because the media persona has no PTZ at all, Protect and Frigate cannot
contend for the head: Frigate's autotracking is untouched by Protect's
presence, and Protect's presence cannot move the camera. The two clients are
independent from the NVR's point of view and independent from each other.

## Non-goals

| Not doing | Why |
|---|---|
| Letting Protect drive PTZ | That is a different feature and already possible: give the `protect` principal the `ptz` role and the existing arbitration applies. This feature is the opposite: Protect never gets the head. |
| Native UniFi adoption of the physical G5 | The G5 has one native controller relationship. A second native controller cannot subscribe to the same session, and custody moves are an operational action outside software validation. |
| Recording in Osprey | Osprey re-publishes media; the NVR records it. |
| A per-camera media persona | One media persona serves the adopted camera. Multi-camera deployments get one media persona per adopted camera on the same port, addressed by camera. |

## Verification

Without a camera:

- WS-Discovery answers a Probe with one ProbeMatch per persona, each with its
  own endpoint UUID, XAddr and scopes.
- The media persona's ProbeMatch carries no PTZ scope.
- A PTZ-scoped Probe is answered by the primary persona alone.
- The two-persona message fits in one datagram.
- The media persona reports a serial and a MAC that differ from the primary
  persona's and from the physical camera's, and never the primary's operator
  overrides.
- The media persona's `GetServices` omits PTZ; its `GetProfiles` carries no
  PTZ configuration; every PTZ operation faults.

With the camera (`osprey/tests/test_stack.py` and the live deployment):

- The stack announces both personas, the media persona first, and the primary
  claims PTZ once a camera is adopted.
- Frigate keeps its autotracking PTZ while the media persona is serving a
  second client.
- The camera head never moves because of the media persona.
