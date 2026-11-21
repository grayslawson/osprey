# Frigate

Osprey is not an NVR. Run Frigate as you already do, and add Osprey to it as
an ONVIF camera with an RTSP stream. Use Osprey's address, not the physical
camera's. Frigate sends PTZ commands to Osprey, and Osprey moves the camera
over its private control channel.

## Camera entry

```yaml
cameras:
  driveway_ptz:
    ffmpeg:
      inputs:
        - path: rtsp://osprey:RTSP_PASSWORD@192.0.2.10:8554/video2
          roles: [detect, record]
    onvif:
      host: 192.0.2.10
      port: 8000
```

The tracks are `video1` (2688×1512), `video2` (1280×720) and `video3`
(640×360), all H.264 by default. Leave the RTSP credentials out if RTSP
authentication is off. If ONVIF authentication is on, add `onvif.user` and
`onvif.password` and keep `onvif.tls_insecure: false`; see
[security](security.md#onvif-authentication).

Labels, zones, detectors, recording, retention, autotracking and zoom are
configured in Frigate. Osprey never changes them.

## Frigate URL in the console

In **Settings → Frigate**, enter the Frigate base URL, such as
`http://frigate.example.com:5000`. Osprey uses it for the Health check and a
link. It is not a control channel and grants no access to Frigate. See
[Frigate connection settings](frigate-connection.md).

## Check the connection

1. Frigate can reach Osprey's ONVIF port and the RTSP stream decodes.
2. A `home` preset exists, or another preset you name in `return_preset`.
3. Autotracking works in a supervised test.
4. Osprey's Health tab and logs show no refused or contended moves.

A healthy Osprey does not mean Frigate is detecting or tracking correctly.
Check Frigate's own logs too.

## Presets

ONVIF `GetPresets` returns every saved preset. Presets persist across
restarts, and if no `home` exists Osprey saves the camera's first confirmed
position as `home`, so `return_preset: home` works out of the box. Turn this
off with `ptz.auto_home: false`; see [PTZ health](ptz-health.md).

Frigate reads the preset list when it initialises the camera and keeps it in
memory. After you add or rename a preset in Osprey, restart Frigate or save
the camera's ONVIF settings in Frigate to reload it; its log reports how many
presets it found. Frigate also lower-cases preset names, so names that differ
only in case collide. Use names such as `home`, `back-porch` and
`lower-gate`, and set `onvif.autotracking.return_preset` to the exact one.

## Manual control and autotracking

When a person moves the camera from the console, MQTT or a non-autotracker
ONVIF user, Osprey holds back Frigate's PTZ commands for `ptz.manual_hold_sec`
(default 30 seconds) and then lets autotracking resume. Give Frigate its own
ONVIF user and keep it in `ptz.autotrack_users` (default `["frigate"]`).
Anonymous ONVIF callers always count as autotrackers. The details are in
[UniFi Protect alongside Frigate](protect-dual-adoption.md#letting-protect-move-the-camera).

If you run a [sentry patrol](sentry.md), gate autotracking on a full-frame
zone, as that page explains.
