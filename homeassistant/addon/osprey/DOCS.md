# Osprey

Osprey acts as the controller for a supported UniFi PTZ camera and serves it
as an ONVIF camera with RTSP streams, so an existing Frigate can record it and
move it. This app does not install, bundle or configure Frigate.

## Before you install

- The camera must be able to reach this host on TCP 7442, 7444 and 7550, and
  Frigate must reach it on the ONVIF and RTSP ports. The app uses host
  networking for this.
- The camera can have only one controller. If UniFi Protect has adopted it,
  it must be released before it can connect to Osprey. See the
  [installation guide](https://github.com/grayslawson/osprey/blob/main/docs/installation.md#point-the-camera-at-osprey).

## Installing

1. Open **Settings → Apps → Install app**.
2. In the top-right menu, select **Repositories**.
3. Add `https://github.com/grayslawson/osprey-ha-apps` and select **Add**.
4. Install **Osprey** and set `advertised_host`.
5. Start the app and open it from the sidebar.

Home Assistant derives the app's ID from the repository URL, so the URL will
not change.

## Options

| Option | What it does |
| --- | --- |
| `advertised_host` | Required. This host's address on the camera's network. Osprey gives it to the camera and to ONVIF clients. |
| `camera_ip` | Optional. Reject a camera connecting from any other address. |
| `camera_mac` | Optional. Reject a camera with any other MAC. |
| `camera_name` | Controller name shown to the camera and in discovery. |
| `announce` | Multicast a WS-Discovery Hello so ONVIF clients find Osprey. Off by default; Osprey always answers discovery probes. |
| `tracks` | The video tracks to arm and their codecs, such as `video1:h264,video2:h265`. Osprey arms exactly these. |
| `onvif_port` | ONVIF and console port. Default 8000. Leave it at 8000: the sidebar panel always connects there. |
| `rtsp_port` | RTSP port. Default 8554. |
| `admin_password` | Optional. Password for the console on the direct port. |

## Console access

Opened from the Home Assistant sidebar, the console is reached through
ingress and Home Assistant has already signed you in. The console on the
direct port (the **Open Web UI** button, port `onvif_port`) asks for
`admin_password`, and stays locked while that option is empty.

## Point Frigate at Osprey

Frigate can be another app or run elsewhere. Configure the camera with:

- **ONVIF**: host `advertised_host`, port `onvif_port`.
- **RTSP**: `rtsp://<advertised_host>:<rtsp_port>/video1`, or another track
  from `tracks`.

ONVIF and RTSP are anonymous in this app, so firewall those ports to Frigate
and the camera. See the
[Frigate guide](https://github.com/grayslawson/osprey/blob/main/docs/frigate-integration.md).

If you use autotracking with a patrol, gate it on a zone that covers the whole
frame; Frigate zones move with the picture. Osprey needs no custom
integration, and Home Assistant's built-in ONVIF integration can use the same
endpoint.

## Data

The certificate the camera trusts, presets and patrols are kept in
`/data/osprey`.

## Known limitations

- Only the UniFi G5 PTZ is fully supported. Other UniFi PTZ models are
  experimental.
- Images are published for `amd64` and `aarch64` only.
- The ONVIF, RTSP and camera ports have no authentication here. Keep them on
  the camera and Frigate networks and never forward them.

## Help

Report problems at <https://github.com/grayslawson/osprey/issues>. The full
documentation is at <https://github.com/grayslawson/osprey/tree/main/docs>.

## License

MIT. See <https://github.com/grayslawson/osprey/blob/main/LICENSE>.

Osprey is an independent project, not affiliated with or endorsed by Ubiquiti
Inc. UniFi and UniFi Protect are trademarks of Ubiquiti Inc.
