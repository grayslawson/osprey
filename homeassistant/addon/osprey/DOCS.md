# Osprey

Osprey controls a supported Ubiquiti PTZ camera over ONVIF and serves RTSP
streams, so an existing Frigate installation can use the camera. It does not
install, bundle or reconfigure Frigate.

## Before you install

- The camera has to be reachable from this host, and this host has to be
  reachable from the camera: the camera connects to Osprey's ONVIF and RTSP
  ports.
- The camera must be adopted by Osprey. Osprey announces itself when it
  starts; if the camera is already adopted by another controller, turn
  `announce` off and adopt it from the operator console.
- The camera's own firewall has to allow the ONVIF and RTSP ports from this
  host.

## Installing

1. **Settings → Apps → Install app**.
2. In the top-right menu, select **Repositories**.
3. Add `https://github.com/grayslawson/osprey-ha-apps` and select **Add**.
4. Install **Osprey** and set `advertised_host` to the address of this host
   that the camera can reach.
5. Start the app. The operator console is on port 8000, or through the
   ingress link on the app page.

The repository URL is part of the app's identity: the Supervisor derives the
app's repository ID from it. Do not rename or move the repository without
telling anyone who already installed the app.

## Options

| Option | What it does |
| --- | --- |
| `advertised_host` | The address of this host that the camera can reach. The camera connects to Osprey, so this must be an address on the camera's network. |
| `camera_ip` | The camera's IP address. Optional; an identity guard only. |
| `camera_mac` | The camera's MAC address. Optional; an identity guard only. |
| `camera_name` | The name Osprey announces to the camera and shows in the console. |
| `announce` | Whether Osprey announces itself to the camera at startup. |
| `tracks` | The camera's video tracks and codecs, for example `video1:h264,video2:h265`. Osprey reads them from the camera, so this is only needed when the camera will not report them. |
| `onvif_port` | The port the ONVIF service listens on. Frigate connects here. |
| `rtsp_port` | The port the RTSP service listens on. Frigate connects here. |

## Outside the app

Frigate is not installed by this app and is not managed by it. Point Frigate
at Osprey:

- **ONVIF**: `onvif://<camera_ip>:8000` — the camera is reached through
  Osprey, so the address is the camera's and the port is Osprey's.
- **RTSP**: `rtsp://<osprey_host>:<rtsp_port>/video1`.

Osprey needs no custom integration. Home Assistant's built-in ONVIF
integration can use Osprey's ONVIF endpoint directly.

Autotracking has to be enabled in Frigate, and its `required_zones` must name
a zone that covers the whole frame: Frigate's zones are relative to the
frame it is tracking, not to the camera's field of view.

## Known limitations

- Only the Ubiquiti G5 PTZ is supported. Other UniFi PTZ models are
  experimental: Osprey runs, but not every control works.
- The web console declares ingress but does not honour `X-Ingress-Path` yet,
  so open it through its published port until that lands.
- The add-on image is published for `amd64` and `aarch64` only.
- Ingress authenticates the console with Home Assistant, but the directly
  published ports are not independently authenticated. Firewall them to the
  camera and Frigate networks, never port-forward them.

## Getting help

Report problems at <https://github.com/grayslawson/osprey/issues>. The
documentation is at <https://github.com/grayslawson/osprey/tree/main/docs>.

## License

MIT. See <https://github.com/grayslawson/osprey/blob/main/LICENSE>.
