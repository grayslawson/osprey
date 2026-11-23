# Troubleshooting

Start with `osprey doctor` (see [verify a deployment](installation.md#verify-a-deployment))
and the console's **Health** tab. Then find your symptom below. The log
commands assume the release Compose file; use `journalctl -u osprey` or your
own log path elsewhere.

```sh
docker compose -f compose.release.yaml logs --tail 200 osprey
```

## The camera never connects

`/healthz` answers `503` with `"camera": false`, and the log has no
`channel up` line.

- The camera was never pointed at Osprey. Adopting is a separate step from
  starting Osprey. Run `manage.sh` as in
  [point the camera at Osprey](installation.md#point-the-camera-at-osprey).
- `manage.sh` fails to log in. The camera only accepts `ubnt`/`ubnt` while
  it is factory fresh. If another controller adopted it, remove it there and
  reset the camera first.
- Osprey's address is wrong. `OSPREY_HOST` (or `host`) is the address the
  camera dials. It has to be reachable from the camera, so not `localhost`, a
  container bridge address or a wildcard.
- Something blocks the ports. The camera must reach TCP 7442, 7444 and
  7550 on the Osprey host. Check the host firewall, and any VLAN rule between
  the camera and Osprey.
- Protect took it back. If a Protect console can see the camera, one click
  on **Adopt** moves it away from Osprey. Remove the camera from Protect, point
  it back at Osprey, and set up the [Protect guard](protect-guard.md).
- Osprey rejects it. These log lines say why:

  | Log line | Cause |
  |---|---|
  | `rejected unregistered camera: mac=...` | The `cameras` allow-list is set and this MAC is not in it. |
  | `rejecting media ingest from unregistered peer ...` | The camera's IP is not the one you configured (`--expected-camera-ip`, `camera_ip` in the Home Assistant app, or `ip` in `cameras`). |
  | `rejected connection from ...` | The connection did not complete a TLS and WebSocket handshake. The first bytes are in the line. |

Running Osprey in a bridged container is a common cause too. Host networking
is the default for a reason: the camera dials the host address you gave it,
and WS-Discovery multicast does not cross a bridge.

## The camera connects but adoption does not finish

Look for `adopted <mac>` in the log, and for the adoption check in the
console's Health tab.

- A camera that still thinks another controller owns it may send nothing but
  time sync messages and ignore Osprey. Release it from that controller, or
  factory-reset it, then run `manage.sh` again.
- Keep the state volume. The camera trusts the certificate Osprey made on its
  first run (`osprey.pem`). If you lose the volume, expect to point the camera
  at Osprey again.
- A camera model other than the G5 PTZ adopts as experimental. The log says
  `model '...' is EXPERIMENTAL`. That is a warning, not a failure; see
  [camera models](camera-models.md).

## No video

- The console's Live view is black. The Live tab shows an MJPEG preview
  that needs FFmpeg. The container image has it; a wheel install only needs it
  for this preview. Only one browser gets the live preview at a time. A second
  tab shows a snapshot that refreshes every five seconds.
- RTSP answers `503 Service Unavailable` with `Retry-After`. The track is
  armed but the camera has not pushed a frame on it yet. Wait a few seconds,
  and check that port 7550 is open to the camera. `/healthz` reports each
  track's `last_frame_age`. After 30 seconds without a frame on any track the
  status becomes `degraded`.
- A `404` on `rtsp://.../video2`. Osprey only serves the tracks in
  `tracks`. The Home Assistant app arms `video1` and `video2` by default.
- The picture decodes in VLC but not elsewhere. The camera sends H.264 or
  H.265 per track, and Osprey passes it through without transcoding. A client
  that cannot decode H.265 needs an H.264 track. Home Assistant's ONVIF
  integration refuses an H.265 only device with "no H.264 streams available".
  Set a track to `h264` in `tracks`, or from the console's **Settings** tab.
- Frigate says the stream has no video. Test the same URL with
  `ffprobe -rtsp_transport tcp rtsp://osprey:RTSP_PASSWORD@192.0.2.10:8554/video2`
  from the Frigate host. If that works, the fault is in Frigate's input
  settings. If RTSP authentication is on, the username and password have to
  match the wizard's output.

## `PTZ motor state timeout`

Osprey logs `PTZ motor state timeout` (a warning) when a move has run for 15
seconds without the camera saying it stopped. The state goes to `UNKNOWN` and
Osprey asks the camera for its position. The first answer clears it.

If it clears by itself and does not come back, it is nothing to fix. If it
keeps coming back:

1. Press **Recover PTZ** on the Live tab. It recycles the PTZ channel at once.
2. Check `GET /healthz`. `ptz.unanswered_position_requests` and
   `ptz.recovery` show whether the watchdog is already recovering the channel.
3. Search the log for `PTZ watchdog:` and `PTZ recovery timed out`. After
   repeated failed recoveries Osprey asks the camera to reboot, at most twice
   an hour. A `camera reboot skipped:` line means the cap was hit.
4. Power cycle the camera if the channel never comes back.

[PTZ health](ptz-health.md) explains the rules behind each step.

## Frigate does not track

Work through these in order. Each rules out one layer.

1. Frigate cannot reach ONVIF. From the Frigate host, `curl -s
   http://192.0.2.10:8000/healthz` should answer. Use the address from your
   Frigate `onvif.host`.
2. Autotracking is not enabled in Frigate. It is off by default, and it
   needs a non-empty `required_zones`. See [Frigate](frigate-integration.md#autotracking).
3. The camera is not a G5 PTZ. Frigate needs relative moves within the
   field of view. Osprey only has the lens calibration for the G5 PTZ. On other
   models those moves are off, so autotracking may not work.
4. Someone moved the camera by hand. A move from the console, MQTT or an
   ONVIF user outside `ptz.autotrack_users` holds Frigate off for
   `ptz.manual_hold_sec` (30 seconds by default). Frigate's commands during
   the hold are acknowledged but not sent.
5. The ONVIF user is not `frigate`. With ONVIF authentication on, only
   users in `ptz.autotrack_users` count as autotrackers. Anonymous callers
   always do.
6. A sentry patrol is fighting the tracker. Gate Frigate autotracking on a
   zone that covers the whole frame, as [sentry](sentry.md) explains.
7. Frigate has a stale preset list. It reads presets once. After you add
   `home`, restart Frigate or save its ONVIF settings again.
8. Moves are refused. Look for `PTZ move refused:` in Osprey's log. It is
   rate limited to once every 30 seconds per reason.

To see every ONVIF call and who made it, set `"onvif": {"log_calls": true}` in
`osprey.json` and restart. Turn it off afterwards, because it is noisy.

## Everything is unhealthy right after the first start

Until a camera is adopted, `/healthz` answers `503` and the container reports
`unhealthy`. That is expected during first run and the setup wizard. It turns
healthy once the camera has connected.

## Discovery does not find Osprey

WS-Discovery is multicast and stays on one network segment. It does not cross
a VLAN, a container bridge or a Wi-Fi network that isolates clients. Add the
device by address instead: host from `OSPREY_HOST`, port 8000 (or 8001 for the
[media persona](protect-dual-adoption.md)).

## Still stuck

Collect `osprey doctor` output and the last 200 log lines. Logs are redacted,
but read them before you post. Then open an issue. If your camera is not a G5
PTZ, run [`osprey probe`](camera-models.md#help-add-a-model-osprey-probe) and
attach its bundle to a **Camera model support** issue.
