# UniFi Protect and Osprey: custody versus re-share

This note describes the supported architecture for a G5 PTZ that is already
adopted by Osprey. It is intentionally conservative: no production controller
or camera is contacted by the tests in this repository.

## Conclusion

The physical G5 has one native UniFi controller relationship. Its native
connection is a TLS WebSocket (`secure_transfer`) to Protect on port 7442; the
PTZ channel (`ptz1`), settings acknowledgements, and camera-originated media
all belong to that controller session. Adoption is not a second read-only
subscription. Osprey's custody code also records the observed behavior that
stealing the socket does not transfer ownership: an already-adopted camera
ignores unsolicited controller introductions and returns `Unauthorized` to
settings until it is explicitly released and adopted elsewhere.

Therefore the same physical G5 cannot be natively adopted by Osprey and
simultaneously natively adopted by Protect. Releasing, unadopting, resetting,
or moving the camera to test this is outside the supported production
procedure.

## What can work: a media-only persona

Osprey can remain the sole native controller and re-publish the camera's
already-ingested tracks as ordinary RTSP and ONVIF. The existing server does
this at the configured ONVIF (default `8000`) and RTSP (default `8554`) ports.
An independent NVR may consume that persona, for example Frigate. This does
not create a second G5 controller and does not grant the consumer native
Protect custody.

Protect's recent releases include third-party ONVIF camera adoption, but
Ubiquiti's exact firmware/version and profile requirements are not stable
enough for Osprey to promise compatibility from protocol inference alone. A
Protect controller could potentially adopt Osprey's ONVIF persona as a
*separate generic camera*; that is a Protect-to-Osprey interoperability test,
not dual adoption of the G5. Protect must not be pointed at the G5's native IP
expecting ONVIF, because the G5's native protocol is not ONVIF/RTSP.

If Protect is used as a consumer, keep PTZ writes disabled until an explicit,
supervised compatibility test proves what it does with the proxy's ONVIF PTZ
surface. Osprey's normal arbitration and authentication boundaries still
apply. Never expose these ports beyond a trusted LAN.

## Evidence in this repository

* [`pyunifiwire/SPEC.md`](../pyunifiwire/SPEC.md) records the measured 7442
  WebSocket, second `ptz1` channel, camera-pushed 7550 media, and 7444 snapshot
  paths.
* [`cuckoo/ARCHITECTURE.md`](../cuckoo/ARCHITECTURE.md) documents exclusive
  custody and the ONVIF/RTSP northbound re-share.
* [`cuckoo/tests/test_stack.py`](../cuckoo/tests/test_stack.py) exercises the
  fake-camera-to-Osprey session and then reads the ONVIF and RTSP faces without
  hardware.

## Safe validation

Run `cuckoo/test.sh` (or `pytest -q cuckoo/tests`) and use the fake stack tests.
For a real deployment, validate only by reading Osprey's operator status and
connecting a disposable RTSP/ONVIF client to Osprey's advertised endpoints.
Do not run `custody.py release`, `restore`, adoption, reset, or PTZ commands on
the production camera for this question.

