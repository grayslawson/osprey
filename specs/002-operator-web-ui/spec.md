# Operator web UI

Status: phases 0 to 3 implemented; phases 4 to 7 planned.
Design notes: `specs/002-operator-web-ui/` and the contract in
[`contracts/http-api.md`](contracts/http-api.md).

## User outcome

An operator can see the camera's state, including a stuck PTZ motor, and move
it by clicking, dragging, zooming, using a joystick or the keyboard, and manage
presets without reloading the page. The same app works from Home Assistant
ingress, on a phone, and in light or dark themes, with no CDN or build step.

## Scope by phase

| Phase | Scope | Status |
|---|---|---|
| 0 | Camera-scoped routes, CSRF header, Secure cookies over TLS, CSP and framing headers, logout, non-200 failed login, preset save and delete without reload, password required by default, read timeouts, login throttle | Done. Tests in `cuckoo/tests/test_webapi.py` and `test_onvif.py`. |
| 1 | `/api/v1` JSON, SSE `/api/v1/events`, `recover_hook` for the PTZ watchdog | Done. The watchdog branch installs `recover_hook` or `Backend.recover_ptz()`. |
| 2 | No-build Preact/htm/signals app in `cuckoo/web/`, vendored with pins, licences and checksums, CSP-safe, light and dark, mobile, WCAG basics | Done. Browser-checked (Chrome, phone width) but not run through axe or Lighthouse. |
| 3 | Click-to-center, drag, wheel and pinch zoom, joystick, keys, preset gallery with thumbnails, PTZ health card with Recover | Done. Gestures checked in headless Chrome against the fixture camera; the committed tests do not drive the browser. |
| 4 | Live video: MJPEG fan-out, WebCodecs over WebSocket, go2rtc option | Not started. Preview is still single-viewer MJPEG with a snapshot fallback. |
| 5 | Health checks and wizard with discovery and adoption tracker | Health checks and recover actions are done. The wizard is not redesigned. |
| 6 | Patrol, lease pill, Frigate snippet, backup and restore, multi-camera grid | Not started. The Frigate URL is editable. |
| 7 | Privacy zones | Not started. |

## Acceptance checks

- Motor timeout shows `UNKNOWN` with the error on the headline, in the health
  list, and in the app's PTZ health card (`test_motor_timeout_is_reported_as_unknown_everywhere`).
- A multi-camera request reaches the selected camera (`test_camera_scoped_legacy_controls_move_the_selected_camera`).
- A mutation without `X-CSRF-Token` returns `403` (`test_mutations_require_the_csrf_header`).
- Home Assistant ingress paths drive redirects, cookies and the import map
  (`test_ingress_prefix_is_honoured_for_redirects_pages_and_cookies`,
  `test_index_is_served_with_strict_csp_and_static_assets`).
- The vendored modules match their manifest (`test_vendor_manifest_matches_the_vendored_files`).

## Known limits

- Live video is still MJPEG with one viewer at a time; a second tab gets the
  snapshot fallback.
- Continuous motion is simulated with bounded steps, so a held input moves in
  pulses rather than smoothly.
- Click-to-center needs a field-of-view calibration for the camera model.
  Without one, the app disables it and says so.
- The browser gesture checks are manual, not part of `make test`.
