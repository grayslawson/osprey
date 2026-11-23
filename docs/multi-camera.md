# Multiple cameras

One Osprey process can serve several cameras. List each one's MAC, display
name and optional IP hint in `osprey.json`:

```json
{
  "host": "osprey.local",
  "cameras": [
    {"mac": "AA:BB:CC:DD:EE:FF", "name": "Driveway", "ip": "192.0.2.40"},
    {"mac": "11:22:33:44:55:66", "name": "Back Yard"}
  ]
}
```

MACs are normalised and must be unique; separators are optional. With a list
present, Osprey rejects any camera not on it. Without one, it accepts any
camera. `name` is shown to ONVIF clients, and `ip` is never the
camera's identity, but when it is set Osprey accepts media and snapshot
uploads only from that address. Setting it is recommended; see
[security](security.md#the-cameras-address).

The console has a camera switcher. Preview, status, PTZ, presets, patrol and
the Frigate URL follow the selected camera. Each camera gets a stable ID from
its MAC, such as `g5-ptz-aabbccddeeff`.

Camera-scoped routes:

```text
/cameras/{camera-id}/                 (opens the app for that camera)
/cameras/{camera-id}/legacy
/cameras/{camera-id}/preview/{track}
/cameras/{camera-id}/snapshot/
/cameras/{camera-id}/control/step
/cameras/{camera-id}/control/home
/cameras/{camera-id}/control/preset
/cameras/{camera-id}/control/zoom
/cameras/{camera-id}/api/frigate
/api/v1/cameras/{camera-id}/state    (see docs/operator-ui.md)
```

An unknown or unadopted camera ID returns `404`; Osprey never falls back to
another camera. Preview locks, movement leases, presets and controller
sessions are separate per camera. The unscoped routes are for a single-camera
deployment. [Sentry](sentry.md) keys patrols by camera MAC.
