# Connecting an existing Frigate installation

Osprey is a controller for supported UniFi PTZ cameras, not an NVR. Install
and operate Frigate independently, then add Osprey as an ONVIF/RTSP camera.

## Network contract

Use the Osprey host address in Frigate, not the physical camera's Protect
address. Frigate sends ONVIF PTZ commands to Osprey; Osprey translates them to
the camera's private control channel and re-publishes the camera's media.

```yaml
# Existing Frigate configuration — addresses are examples.
cameras:
  driveway_ptz:
    onvif:
      host: osprey.local
      port: 8000
    ffmpeg:
      inputs:
        - path: rtsp://osprey.local:8554/video2
          roles: [detect, record]
```

Configure labels, zones, detectors, recording, retention, autotracking mode,
and zoom behavior in Frigate. Those choices belong to the Frigate owner;
Osprey never rewrites them.

## Frigate URL in the Osprey console

In **Settings → Frigate**, enter the base URL of the Frigate instance associated
with the selected camera, such as `http://frigate.local:5000`. This is used for
health feedback and a convenient operator link; it is not a remote-control
channel and does not grant Osprey permission to modify Frigate. Use a complete
`https://` URL when TLS is enabled and do not embed credentials in the URL.

## Verify the connection

1. Confirm Frigate can reach Osprey's ONVIF endpoint and selected RTSP stream.
2. Confirm the stream appears in Frigate and decodes correctly.
3. Save at least one Home or named Osprey position before enabling automatic
   return-to-home behavior.
4. Enable and tune Frigate autotracking with a supervised test.
5. Review Osprey camera status and control-event history for rejected or
   contended moves before leaving the system unattended.

Healthy Osprey status does not prove Frigate detection or tracking is correct;
inspect Frigate's own logs and event pipeline. For the Protect custody
limitation, see [Protect and Osprey](protect-dual-adoption.md).

## Named-position discovery and refresh

Osprey returns every saved named position from the ONVIF `GetPresets` response;
the `home` position is not privileged on the wire. Frigate discovers that list
when its ONVIF controller initializes a camera and then keeps the list in memory.
Consequently, a position saved in Osprey after Frigate connected will not appear
in Frigate's live-view preset menu immediately.

After adding or renaming positions in Osprey, restart Frigate or trigger an
ONVIF camera reinitialization (for example, save the camera's ONVIF settings in
Frigate). Reloading only the browser page is not sufficient if Frigate's
ONVIF controller is still initialized. Verify the refresh in Frigate's logs;
it should report the number of presets found. Osprey's operator console and an
ONVIF client such as the included `harness/ptz_walk.py` show the authoritative
list served by Osprey.

Frigate normalizes preset names to lower case for its menu and command matching,
so names that differ only by capitalization are treated as one position. Keep
names unique (for example, `home`, `lower-loitering`, and `back-porch`) and set
`onvif.autotracking.return_preset` to the exact name you want Frigate to use.

This behavior follows Frigate's ONVIF controller implementation, which loads
presets during camera initialization: [Frigate PTZ ONVIF controller](https://github.com/blakeblackshear/frigate/blob/dev/frigate/ptz/onvif.py).
