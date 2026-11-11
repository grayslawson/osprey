# Camera models

| Model | Status | Notes |
|---|---|---|
| UniFi G5 PTZ (`UVC G5 PTZ`, model id `0xa59b`) | Supported | Tested end to end: adoption, RTSP, ONVIF PTZ, FOV-relative moves, presets, Frigate autotracking. |
| Other UniFi PTZ cameras | Experimental | Adoption and streaming use the same protocol. PTZ uses the pan, tilt and zoom ranges the camera reports. FOV-relative moves (`TranslationSpaceFov`) and click-to-center are off. |
| UniFi cameras without PTZ | Not a goal | They may stream, but Osprey exists for PTZ. |

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

2. Build the bundle:

   ```sh
   osprey probe --camera 192.0.2.20 --dump /tmp/osprey-messages.jsonl --out probe.json
   # container: docker compose exec osprey /usr/local/bin/osprey-entrypoint probe ...
   # source checkout: ./scripts/osprey probe ...
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
