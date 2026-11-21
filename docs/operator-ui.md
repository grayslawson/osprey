# Operator console

Osprey serves a browser app at `/` on the ONVIF port (8000 by default). It
needs an admin password; see [authentication](authentication.md). The app
has no build step. Preact, htm and signals are vendored under
`osprey/web/vendor` with pinned versions, licences and checksums, and nothing
loads from a CDN.

## Routes

| Path | What it is |
|---|---|
| `/` | The app. Redirects to `/setup` while setup is pending. |
| `/cameras/{id}/` | The app, opened on one camera. |
| `/legacy` | The older server-rendered console. |
| `/api/v1/...` | JSON API. Contract: [`specs/002-operator-web-ui/contracts/http-api.md`](../specs/002-operator-web-ui/contracts/http-api.md). |
| `/api/v1/events?camera={id}` | Server-sent events: `state`, `ptz`, `streams`, `health`, `preset`, `adoption`, `log`, `session`. |

The app has five tabs: **Live**, **Health**, **Logs**, **Settings** and
**Patrol**. It works at phone width, follows the system light or dark theme
(the header button overrides it), and does not rely on colour alone to show
status.

## Live

- Click or tap the picture to centre on that point. This needs a
  field-of-view calibration for the camera model, which only the G5 PTZ has.
- Drag to pan; releasing stops the camera.
- Scroll or pinch to zoom.
- Use the joystick or the arrow buttons. The step slider sets how far one
  press moves.
- Keys: arrows or W A S D pan while held (Shift is faster), `+` and `-` zoom,
  `H` goes home, Space stops, `1` to `9` go to presets, and `?` lists them
  all.
- The PTZ health card explains an unknown motor state and offers
  **Recover PTZ**.

Presets are managed on the same tab. You can save the current view as a new
preset, update one to the current view, go to one, or delete one after
confirming. Each preset shows a thumbnail from when it was last saved.

The video is the camera's MJPEG preview, which needs FFmpeg on the host. Only
one viewer gets the live preview at a time; a second tab shows a snapshot that
refreshes every five seconds.

## Patrol

Configure and watch the [sentry patrol](sentry.md#operator-console) for the
selected camera.

## Health, Logs and Settings

**Health** lists checks for adoption, the PTZ channel, motor state, the
watchdog, the Frigate connection and ONVIF authentication (see
[PTZ health](ptz-health.md)). **Logs** shows a live, redacted tail of recent
log records, filterable to warnings or errors. **Settings** shows the camera's
model and firmware, lets you change each track's codec, and holds the
[Frigate URL](frigate-connection.md).

## Home Assistant ingress

Every URL in the app is relative, so it works under Home Assistant's ingress
path. Cookies and CSRF tokens follow that path. See
[authentication](authentication.md#home-assistant-ingress).

## Checking the vendored files

```sh
python3 scripts/verify-web-vendor.py          # offline: checksums and file list
python3 scripts/verify-web-vendor.py --fetch  # also re-download the npm tarballs
```

`make test` runs the offline check.

## Not yet built

WebCodecs live video, the Frigate or go2rtc player, privacy zones, backup and
restore, and a redesigned setup wizard. They are listed in
[the spec](../specs/002-operator-web-ui/spec.md).
