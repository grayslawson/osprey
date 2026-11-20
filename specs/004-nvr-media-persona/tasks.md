# Tasks: NVR media persona

## 1. Identity

- [x] `identity.resolve()` takes a `persona` marker and seeds the derived
      serial, MAC and hostname with it.
- [x] A persona does not inherit the primary's operator overrides or the
      discovery interface's MAC — it must be a distinct device.
- [x] The persona's identity is never the physical camera's serial or MAC.
- [x] Tests: two personas over one camera seed differ in serial, MAC and
      hostname; the camera's own identifiers are still never reported.

## 2. ONVIF services

- [x] `Services` takes a validated `persona` argument.
- [x] `device_identity()` passes it to `identity.resolve()`.
- [x] The friendly name and the advertised scopes carry the persona.
- [x] Tests: the media persona reports its own serial and a distinct name.

## 3. Discovery

- [x] `probe_match()` emits one ProbeMatch per identity.
- [x] `announcement()` emits one Hello (or Bye) per identity.
- [x] `DiscoveryServer` holds a tuple of identities; `identity` stays as the
      first one.
- [x] A Probe that names scopes is answered by the personas that satisfy all
      of them, and by all of them when it names none.
- [x] The message stays inside one datagram: optional parts are given up in a
      documented order, and a single-persona message is unchanged.
- [x] Tests: two ProbeMatches with their own endpoint UUIDs, XAddrs and
      scopes; a PTZ-scoped Probe gets the primary persona alone; the
      two-persona message fits the budget.

## 4. Wiring

- [x] `main.py` builds the media persona's discovery identity when
      `ports.onvif_read_only` is set, and advertises it first.
- [x] The media persona's services carry the persona marker.
- [x] The primary persona is unchanged.
- [x] Tests: the stack announces both personas and claims PTZ on the primary
      once a camera is adopted.

## 5. Documentation

- [x] `docs/protect-dual-adoption.md`: the adoption steps, the two entries a
      client sees, and how to tell them apart.
- [x] `docs/installation.md`: where the media persona is configured.

## 6. Verification

- [x] The full gate passes.
- [ ] The live deployment serves the media persona and advertises both.
