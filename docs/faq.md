# FAQ

## Do I need Frigate?

No. Osprey serves the camera as a standard ONVIF device with RTSP streams, so
VLC, `ffplay` and Home Assistant's ONVIF integration work with it too.
Frigate is what Osprey was built for, because Frigate's autotracking needs an
ONVIF PTZ camera. Osprey never installs, configures or runs Frigate.

## Why can't Frigate talk to the camera directly?

A UniFi PTZ camera does not speak ONVIF or RTSP to its controller. It talks a
private protocol to UniFi Protect: a JSON WebSocket for control, and a pushed
FLV style video stream. Osprey plays the controller's part, then re-serves the
camera's video and motion as ONVIF and RTSP.

## Do I lose UniFi Protect?

For that camera, Protect stops managing it natively. A camera has one
controller at a time, so you have to remove it from Protect, factory-reset it
and point it at Osprey. See [Uninstall and return to Protect](uninstall.md)
to go back, or [Protect alongside Frigate](protect-dual-adoption.md) to keep
Protect recording the camera as a separate, media only ONVIF device.

## Which cameras work?

The UniFi G5 PTZ is the supported model. Other UniFi PTZ cameras adopt and
stream, but their pan, tilt and zoom use the ranges the camera reports and
the field of view moves that Frigate autotracking relies on are off, so they
are experimental. Cameras without PTZ are not a goal. [Camera
models](camera-models.md) explains how to send a probe bundle for a new model.

## Is it safe to expose to the Internet?

No. Keep Osprey's ports on a trusted LAN or VLAN and never forward them. See
[security](security.md) for what each port exposes. Reach the console from
outside through a VPN or an HTTPS reverse proxy.

## Does it transcode video?

No. The camera pushes H.264 or H.265 per track, and Osprey repackages the
stream as RTP without decoding it. The only place Osprey runs FFmpeg is the
console's MJPEG live preview.

## Why does Home Assistant say "no H.264 streams available"?

Home Assistant's ONVIF integration rejects a device whose profiles are all
H.265. Set at least one track to `h264`. The default for every track is
`h264`; the Home Assistant app arms `video1` as H.264 and `video2` as H.265.
See [Frigate integration](frigate-integration.md#codecs).

## Can it run several cameras?

Yes. One process serves several cameras, each with its own presets and patrol.
See [multiple cameras](multi-camera.md).

## Does it need Docker?

No. The container image is the supported default, but Osprey also runs as a
Home Assistant app, a NixOS service and a Python 3.13 wheel. Docker Desktop on
macOS and Windows cannot serve a LAN camera, because the camera has to reach
the host's real address.

## What breaks it?

A camera firmware update can change the protocol. Osprey is beta software
written from observed behaviour, and [NOTICE](../NOTICE) says so. Run your own
tracking test after any camera firmware change.

## What do Cuckoo and Finch mean?

They are the earlier projects Osprey comes from.
[Cuckoo](https://github.com/rjmotion/cuckoo) is a controller that isn't there:
it adopts a G5 PTZ and re-serves it as ONVIF and RTSP. Osprey is that code
carried forward, renamed and extended. [Finch](https://github.com/rjmotion/finch)
is the reverse, a synthetic camera for testing a controller with no hardware.
The wire library both shared is [pyunifiwire](../pyunifiwire/README.md), which
Osprey bundles. The names survive in old file names such as `cuckoo.json`; see
[upgrading](upgrading.md).

## Is Osprey affiliated with Ubiquiti?

No. Osprey is an independent project. Ubiquiti, UniFi and UniFi Protect are
trademarks of Ubiquiti Inc., used here only to name the hardware Osprey works
with. [NOTICE](../NOTICE) has the full statement.

## Where do I report a bug or a new camera model?

Use the issue forms on GitHub. For security problems, follow
[`SECURITY.md`](../SECURITY.md) instead of opening a public issue.
