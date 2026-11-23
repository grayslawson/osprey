# Camera models

| Model | Status | Notes |
|---|---|---|
| UniFi G5 PTZ (`UVC G5 PTZ`, model id `0xa59b`) | Supported | Tested end to end: adoption, RTSP, ONVIF PTZ, FOV-relative moves, presets, Frigate autotracking. |
| Other UniFi PTZ cameras | Experimental | Adoption and streaming use the same protocol. PTZ uses the pan, tilt and zoom ranges the camera reports. FOV-relative moves (`TranslationSpaceFov`) and click-to-center are off. |
| UniFi cameras without PTZ | Not a goal | They may stream, but Osprey exists for PTZ. |

## Firmware

The G5 PTZ is tested on firmware 5.5.13.67, which is in daily use, and on
5.3.95, the version that the protocol notes in
[`pyunifiwire/SPEC.md`](../pyunifiwire/SPEC.md) were measured on. Other
firmware may work but is untested. Osprey logs a warning for it, because the
camera's protocol can change with any update. After a firmware change, test
adoption, PTZ and Frigate tracking again.

An experimental camera logs this at adoption:

```text
camera … model '…' is EXPERIMENTAL: PTZ uses its reported ranges only and FOV-relative moves are off.
```

The operator API also reports `"support": "experimental"` for the camera.

## Help add a model: `osprey probe`

`osprey probe` is read-only. It sends one UniFi discovery request (UDP 10001)
to the camera and reads a message dump. It never logs in to, configures, moves
or adopts the camera. It writes one JSON bundle in which addresses, MACs,
serials, UUIDs, tokens, names and credentials are replaced by placeholders.

1. Run Osprey with a message dump while the camera is connected, and move the
   camera a little from the Osprey console so the dump has motor-state messages:

   ```sh
   osprey --config osprey.json --dump /tmp/osprey-messages.jsonl
   ```

   In the container, set `OSPREY_EXTRA_ARGS="--dump /tmp/osprey-messages.jsonl"`
   in the service's `environment` and recreate it.

2. Build the bundle:

   ```sh
   osprey probe --camera 192.0.2.20 --dump /tmp/osprey-messages.jsonl --out probe.json
   # source checkout: ./scripts/osprey probe ...
   ```

   In the container, write the bundle to `/tmp`, the only writable path
   besides the state volume, and copy it out:

   ```sh
   docker compose -f compose.release.yaml exec osprey /usr/local/bin/osprey-entrypoint \
     probe --camera 192.0.2.20 --dump /tmp/osprey-messages.jsonl --out /tmp/probe.json
   docker compose -f compose.release.yaml cp osprey:/tmp/probe.json .
   ```

3. Read `probe.json`, then attach it to a **Camera model support** issue.
   Delete the dump afterwards. It is not redacted.

Discovery is best effort. An adopted G5 does not answer unicast discovery
requests, so the dump is the main source of data.

## What a maintainer does with a bundle

1. Check `summary.ptz_steps` and the `EventMotorState` samples against the
   parser in `osprey/ptz.py` (`ranges_from_hello`, `parse_motor_state`).
2. Measure the lens: horizontal and vertical field of view at wide and tele,
   and the mechanical pan and tilt travel in degrees.
3. Add one entry to `SUPPORTED_MODELS` in `osprey/model.py` with that
   `FieldOfView`, add a test with the bundle's hello, and run a hardware test
   with Frigate autotracking before the model moves to Supported.
