# Connecting an existing Frigate installation

Osprey is the controller for supported UniFi PTZ cameras. It is not an NVR and
does not distribute, start, upgrade, or alter Frigate. Install and operate
Frigate independently, then connect that Frigate instance to Osprey as it would
to any ONVIF/RTSP camera.

## Network contract

Osprey exposes one ONVIF device endpoint and RTSP stream per registered camera.
In Frigate, use the Osprey host address and its ONVIF/RTSP ports; do not use the
camera's Protect address as Frigate's PTZ endpoint. Osprey is the component that
receives Frigate's ONVIF moves and sends the corresponding G5 PTZ commands.

```yaml
# Existing Frigate configuration. Addresses and camera names are examples.
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

Configure the labels, zones, detectors, recording, retention, autotracking
mode, and zoom behavior in Frigate. Those choices belong to the Frigate owner
and Osprey never rewrites them.

## Frigate address in Osprey

The operator console's **Settings → Frigate** field takes the base URL of the
Frigate instance associated with the selected camera, such as
`http://frigate.local:5000`. It is used for health feedback and a convenient
operator link. It is not a remote-control channel for Frigate and it does not
give Osprey permission to modify Frigate configuration.

Use `http` only on a trusted LAN. If Frigate is reachable through HTTPS, enter
its complete `https://` base URL and ensure Osprey can validate the certificate.
Do not embed credentials in the URL. Protect the Osprey operator console with
its administrator password and expose neither ONVIF nor RTSP to untrusted
networks.

## Verify the connection

1. Confirm that Frigate can reach Osprey's ONVIF endpoint and selected RTSP
   stream.
2. Confirm that the camera appears in Frigate and that the selected stream
   decodes.
3. Save at least one Home or named Osprey position before enabling automatic
   return-to-home behavior.
4. Enable and tune autotracking in Frigate, starting with a supervised test.
5. Review Osprey's camera status and control-event history for rejected or
   contended moves before leaving the system unattended.

Osprey reports its own endpoint and controller health. A healthy Osprey link is
not proof that Frigate detection or tracking is correctly configured; inspect
Frigate's own logs and events for that part of the pipeline.

For the related UniFi Protect custody limitation and the distinction between
native adoption and a media-only ONVIF/RTSP persona, see
[Protect and Osprey: custody versus re-share](protect-dual-adoption.md).
