# Multiple cameras

One Osprey process can manage several explicitly registered cameras. Configure
the stable camera MAC, a display name, and optionally an IP hint in
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

MAC addresses are normalized and must be unique. When this registry is present,
unregistered control connections are rejected. An empty or omitted `cameras`
list retains single-camera, accept-any-camera compatibility.

The authenticated console shows a camera switcher. Its preview, status, PTZ
controls, presets, and Frigate URL setting are all scoped to the selected
camera. Every registered camera has a stable ID derived from its MAC, for
example `g5-ptz-28704e1ba667`.

```text
/cameras/{camera-id}/status
/cameras/{camera-id}/preview/{track}
/cameras/{camera-id}/control/step
/cameras/{camera-id}/control/home
/cameras/{camera-id}/control/preset
/cameras/{camera-id}/control/zoom
/cameras/{camera-id}/api/frigate
```

Unknown or unadopted camera IDs receive `404`; Osprey never falls back to a
different camera. Preview locks, movement state, presets, and controller
sessions are isolated per camera. Legacy unscoped routes remain only for a
single-camera deployment.
