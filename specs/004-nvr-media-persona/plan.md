# Plan: NVR media persona

## Problem

Osprey serves the camera over ONVIF and RTSP, and it already has a
read-only ONVIF persona (`ports.onvif_read_only`) that omits PTZ. Two things
stop it from being usable by UniFi Protect:

1. **The media persona is invisible.** `DiscoveryServer` holds a single
   `Identity` and answers a Probe with a single ProbeMatch. The media
   persona is never advertised, so a client that discovers Osprey only ever
   finds the primary endpoint. Protect's "Discover Third-Party Cameras"
   therefore lands on the primary, and the operator has to know to type the
   media port by hand.
2. **The two personas share one device identity.** `device_identity.resolve()`
   is seeded with the camera MAC for every persona, so the media persona
   reports the primary persona's serial and MAC. An NVR that sees the same
   serial on two endpoints can merge them or refuse the second, and it
   cannot tell the media view from the full view.

A third problem only shows up once both personas are advertised:

3. **Two ProbeMatches do not fit in one datagram.** The full, standard
   ProbeMatches message for two personas is 1619 bytes. A 1500-byte Ethernet
   MTU leaves 1472 bytes for the UDP payload; anything larger is split into
   IP fragments, which some networks drop and some clients reassemble badly.

## Approach

### 1. A persona marker in the identity

`identity.resolve()` grows a `persona` argument. When it is set, the
derived serial, MAC and hostname are seeded with the camera seed plus the
persona marker, so the two personas differ.

A persona derives **everything** that identifies the device. It does not
inherit the primary's operator overrides (`onvif.identity.serial`,
`onvif.identity.mac`, `onvif.identity.hostname`) and it does not borrow the
MAC of the discovery interface: those describe one device, and sharing them
would make the two personas indistinguishable to an NVR — which is the very
thing this feature exists to prevent. The manufacturer, model and hardware id
describe the product, and are shared.

The camera's own MAC and serial are still never reported.

### 2. A persona on the ONVIF services

`onvif.Services` grows a `persona` argument (`""` for the primary,
`"media"` for the media persona), validated as a short token. It is used in
three places:

- `device_identity()` passes it to `identity.resolve()`.
- `display_name` carries it, so the friendly name becomes `<name> (media)` —
  what an operator sees in the NVR's camera list.
- `scopes()` uses the friendly name, so the advertised name scope matches.

The primary persona is unchanged: its hostname still comes from the operator
override or the controller name, exactly as before.

### 3. Discovery answers for every persona

`discovery.probe_match()` and `discovery.announcement()` take a sequence of
identities and emit one ProbeMatch (or Hello, or Bye) per identity.
`DiscoveryServer` holds `identities: tuple[Identity, ...]` instead of a
single `identity`.

The scope filter follows: a Probe that names scopes is answered by the
personas that satisfy *every* requested scope, so a client that asks for a
PTZ scope gets the primary persona alone; a client that asks for no scope
gets both.

`DiscoveryServer.identity` becomes a property returning the first identity,
so existing callers and tests keep working.

### 4. One datagram

`discovery` grows `MAX_MESSAGE_BYTES = 1472` — a 1500-byte MTU minus a
20-byte IPv4 header and an 8-byte UDP header. `probe_match()` and
`announcement()` render the message and, if it does not fit, give up the
optional parts in a fixed order: the metadata version (optional in the
WS-Discovery schema), the device type (the NVT type alone still identifies
an ONVIF device), the `To` header (optional in a response), the XML
declaration (optional in XML; UTF-8 is the default) and finally the hardware
scope, which only repeats the model the device reports in
`GetDeviceInformation`. If even that is not enough the message is sent whole
and a warning is logged once.

A controller that serves one camera still sends the full, standard message:
nothing is given up unless it has to be.

### 5. Wiring

`main.py` builds the media persona's discovery identity when
`ports.onvif_read_only` is set, and passes both to the discovery server. The
media persona's `Services` gets `persona="media"`.

The **media persona is advertised first**. A client that takes the first
match adopts the one that cannot move the camera, which is the safe default
for the recorder that discovers Osprey. The primary persona keeps
`persona=""`.

## Risks

| Risk | Mitigation |
|---|---|
| A second ProbeMatch confuses an existing client | The extra ProbeMatch only appears when `onvif_read_only` is configured, which is opt-in. Clients that do not want it can ignore the entry. |
| The media persona is offered to a client that wants PTZ | A persona without the PTZ scope never satisfies a PTZ-scoped Probe, so such a client is answered by the primary persona alone. |
| An NVR merges the two personas anyway | Different serial, MAC, hostname, friendly name and endpoint UUID. The media persona's identity is never equal to the physical camera's, so it cannot merge with the G5 either. |
| The media persona's PTZ surfaces leak | `ptz_enabled` is false for a read-only persona regardless of the caller, and the PTZ scope is absent from the advertised scopes. Covered by tests. |
| The two-persona message is fragmented | The message is kept inside one datagram by giving up optional parts in a documented order. Covered by a test. |

## Files

| File | Change |
|---|---|
| `osprey/identity.py` | `resolve()` takes a `persona` marker; a persona derives its own serial, MAC and hostname. |
| `osprey/onvif.py` | `Services` takes a `persona`; identity, friendly name and scopes follow it. |
| `osprey/discovery.py` | `probe_match()`, `announcement()` and `DiscoveryServer` take several identities; the message is kept inside one datagram. |
| `osprey/main.py` | Build and advertise the media persona, first. |
| `osprey/tests/test_discovery.py` | Multi-identity ProbeMatch, scope filtering. |
| `osprey/tests/test_media_persona.py` | The persona's identity, its media surface, the discovery budget, the stack wiring. |
| `docs/protect-dual-adoption.md` | The adoption steps. |

## Verification

```sh
export PYTHONPATH="$PWD/osprey:$PWD/pyunifiwire/src:$PWD/osprey/tests"
python3 -m pytest -q osprey/tests
python3 -m mypy --config-file osprey/mypy.ini osprey
python3 -m ruff check .
./scripts/validate-repo.sh
```
