# Multiple cameras

One Osprey process can manage several explicitly registered cameras. Configure
each camera's stable MAC address, display name, and optional IP hint in
`cuckoo.json`:

```json
{
  "host": "osprey.local",
  "cameras": [
    {"mac": "AA:BB:CC:DD:EE:FF", "name": "Driveway", "ip": "192.0.2.40"},
    {"mac": "11:22:33:44:55:66", "name": "Back Yard"}
  ]
}
```

MAC addresses are normalized and must be unique. When a registry is present,
unregistered camera connections are rejected. An omitted or empty `cameras`
list retains the single-camera, accept-any-camera compatibility behavior.

The authenticated console provides a camera switcher. Preview, status, PTZ
controls, presets, and the Frigate URL are scoped to the selected camera. Each
camera receives a stable ID derived from its MAC, such as
`g5-ptz-aabbccddeeff`.

Camera-scoped API routes are:

```text
/cameras/{camera-id}/status
/cameras/{camera-id}/preview/{track}
/cameras/{camera-id}/control/step
/cameras/{camera-id}/control/home
/cameras/{camera-id}/control/preset
/cameras/{camera-id}/control/zoom
/cameras/{camera-id}/api/frigate
```

Unknown or unadopted camera IDs return `404`; Osprey never falls back to a
different camera. Preview locks, movement state, presets, and controller
sessions are isolated per camera. Legacy unscoped routes remain only for a
single-camera deployment.
