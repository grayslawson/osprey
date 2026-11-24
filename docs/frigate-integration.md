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
      user: frigate
      password: ONVIF_PASSWORD
```

The setup wizard shows this block with your address, ports and passwords
filled in, and can [add it to Frigate](installation.md#add-the-camera-to-frigate)
for you. To write it by hand: new installs require both logins. The RTSP user
is `osprey`, the ONVIF user is `frigate`, and the passwords are the ones the
wizard showed, kept in `rtsp-password` and `onvif-password` in the state
directory. The ONVIF user has to be `frigate` (or a name in
`ptz.autotrack_users`) for autotracking to work. Credentials generated before
0.9.0b7 use the ONVIF user `osprey`; the
[installation page](installation.md#onvif-authentication) shows how to rename
it. An install that predates secure mode may still be anonymous, in which case
drop the credentials. See [security](security.md).

Osprey's three tracks are `video1` (2688×1512), `video2` (1280×720) and
`video3` (640×360). Leave the credentials out only if the matching authentication is off. Keep
`onvif.tls_insecure: false`; see [security](security.md#onvif-authentication).

### Detect and record on different tracks

Frigate does not have to detect on the stream it records. A common setup is to
record the full resolution track and detect on a smaller one, which takes less
CPU. Give each its own input:

```yaml
cameras:
  driveway_ptz:
    ffmpeg:
      inputs:
        - path: rtsp://osprey:RTSP_PASSWORD@192.0.2.10:8554/video1
          roles: [record]
        - path: rtsp://osprey:RTSP_PASSWORD@192.0.2.10:8554/video3
          roles: [detect]
    detect:
      width: 640
      height: 360
```

Whichever track you use, it has to be one that Osprey arms. See
[codecs](#codecs) for the defaults, and note that Frigate's `detect`
dimensions must match the track.

### Codecs

Each track is H.264 or H.265, set in `tracks` (see
[configuration](configuration.md)) or from the console's **Settings** tab.
Osprey passes the camera's stream through without transcoding, so the track's
codec is the codec Frigate receives.

| Where | Default |
|---|---|
| Container, NixOS, wheel | `video1`, `video2` and `video3`, all H.264 |
| Home Assistant app | `video1:h264,video2:h265` |

The Home Assistant app arms only those two tracks, so `video3` does not exist
there and `video2` is H.265. If you follow the examples above in the app,
use `video1` for both roles, or change the app's `tracks` option to
`video1:h264,video2:h264,video3:h264`.

Keep at least one H.264 track. Home Assistant's ONVIF integration refuses a
device that offers only H.265 ("no H.264 streams available"), and some other
clients cannot decode H.265. H.265 is a choice you make for smaller
recordings, and Frigate decodes it for detection. Test your own player before
you commit to it.

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

## Autotracking

Autotracking is a Frigate setting on the same camera entry. A minimal block,
with a full-frame zone because Frigate needs a non-empty `required_zones`:

```yaml
cameras:
  driveway_ptz:
    zones:
      ptz_viewport:
        coordinates: 0,0,1,0,1,1,0,1
    onvif:
      host: 192.0.2.10
      port: 8000
      user: frigate
      password: ONVIF_PASSWORD
      autotracking:
        enabled: true
        track: [person]
        required_zones: [ptz_viewport]
        return_preset: home
```

Frigate's autotracking documentation covers the other keys, such as
`zooming` and `timeout`. Autotracking relies on relative moves within the
field of view. Osprey only has the lens calibration for the G5 PTZ, so on
other models those moves are off and autotracking may not work.

### Calibration

Frigate can measure how fast the PTZ moves, so it predicts where a tracked
object will be after a move. Turn it on for one start, then off again:

```yaml
cameras:
  driveway_ptz:
    onvif:
      autotracking:
        calibrate_on_startup: true
```

Frigate moves the camera in small steps, measures the time, and writes a
`movement_weights` entry into its own configuration. After that, set
`calibrate_on_startup` back to `false`. Frigate's
[autotracking documentation](https://docs.frigate.video/configuration/autotracking)
describes the process.

Frigate's calibration moves reach Osprey as ordinary autotracker commands.
Osprey drops them during a manual hold, so nobody should touch the camera from
the console, MQTT or another ONVIF client while it runs. Stop any
[sentry patrol](sentry.md) first, make sure the `home` preset exists, and
watch the first calibration so you can stop it if the camera does something
odd. Calibrate again if you change `ptz.max_speed`, because it changes the speed Osprey sends.

## Manual control and autotracking

When a person moves the camera from the console, MQTT or a non-autotracker
ONVIF user, Osprey holds back Frigate's PTZ commands for `ptz.manual_hold_sec`
(default 30 seconds) and then lets autotracking resume. Give Frigate its own
ONVIF user and keep it in `ptz.autotrack_users` (default `["frigate"]`).
Anonymous ONVIF callers always count as autotrackers. The details are in
[UniFi Protect alongside Frigate](protect-dual-adoption.md#letting-protect-move-the-camera).

If you run a [sentry patrol](sentry.md), gate autotracking on a full-frame
zone, as that page explains.
