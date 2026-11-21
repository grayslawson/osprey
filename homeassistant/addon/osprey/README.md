# Osprey

Serve a UniFi PTZ camera to Frigate over ONVIF and RTSP.

Osprey takes the place of the UniFi controller for the camera. Frigate gets a
standard ONVIF camera with pan, tilt, zoom, presets and autotracking, and RTSP
streams of each encoder channel. The operator console opens from the Home
Assistant sidebar, with live view, presets, an idle patrol, settings, logs and
health.

The UniFi G5 PTZ is fully supported; other UniFi PTZ models are experimental.
Frigate is not included: run it as another app or on another host.

This is a beta. See the **Documentation** tab for setup, and the
[Osprey repository](https://github.com/grayslawson/osprey) for the full
guides.

Osprey builds on [Cuckoo](https://github.com/rjmotion/cuckoo) by rjmotion and
contributors.
