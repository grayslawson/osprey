# Operator app

Osprey serves a browser app at `/` on the ONVIF port (8000 by default). It is
a static, no-build application: Preact, htm and signals are vendored under
`cuckoo/web/vendor` with pinned versions, licences and checksums, and nothing
is loaded from a CDN.

## Where things are

| Path | What it is |
|---|---|
| `/` | The operator app. Redirects to `/setup` until setup is complete. |
| `/cameras/{id}/` | Opens the app on that camera (`/#/live?cam={id}`). |
| `/legacy` | The previous server-rendered console, kept for fallback. |
| `/api/v1/...` | JSON API used by the app. Contract: [`specs/002-operator-web-ui/contracts/http-api.md`](../specs/002-operator-web-ui/contracts/http-api.md). |
| `/api/v1/events?camera={id}` | Server-sent events: `state`, `ptz`, `streams`, `health`, `preset`, `adoption`, `log`, `session`. |

Sections are **Live**, **Presets**, **Health**, **Logs** and **Settings**.
The app works at phone width, follows the system light or dark theme (the
header button overrides it), and keeps status readable without colour.

## Controlling the camera

On the Live view:

- **Click or tap** the picture to center on that point. This needs a
  field-of-view calibration for the camera model; without it the app says so
  and you can use the other controls.
- **Drag** to pan while held. The picture follows the pointer, and release
  stops the camera.
- **Mouse wheel** or **pinch** to zoom.
- **Joystick** and the **Up / Left / Right / Down** buttons. The step size slider
  sets how far one button press moves.
- **Keys**: arrows or W A S D pan while held (Shift is faster), `+` and `-`
  zoom, `H` goes Home, Space stops, `1` to `9` go to presets, and `?` lists
  every key.
- The **PTZ health** card shows an unknown motor state in plain language and
  offers **Recover PTZ**.

Presets can be saved from the current view, updated to the current view, sent
to, and deleted (after confirmation). Each preset shows a thumbnail captured when
it was last saved or updated.

## Live video

The app plays the camera's MJPEG preview. Only one preview viewer is allowed at a
time, so a second tab shows the snapshot fallback, refreshed every five seconds.

## Home Assistant ingress

Every URL in the app is relative to the page, so the app also works behind the
add-on's ingress path. Cookies and CSRF tokens follow that path. See
[authentication](authentication.md#home-assistant-ingress).

## Verifying the vendored files

```sh
python3 scripts/verify-web-vendor.py          # offline: checksums and file list
python3 scripts/verify-web-vendor.py --fetch  # also re-download the npm tarballs
```

`make test` runs the offline check through `cuckoo/tests/test_webapi.py`.

## Not yet included

Live video over WebCodecs, the Frigate/go2rtc player, patrol and privacy
zones, backup and restore, and the setup wizard redesign are planned phases
and are listed in [the spec](../specs/002-operator-web-ui/spec.md). Use
`/legacy` for the setup-era pages until they land.
