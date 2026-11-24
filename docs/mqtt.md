# MQTT

MQTT is off by default. When enabled, Osprey connects to your broker as a
client; it does not run a broker. The bridge needs the `paho-mqtt` package,
which the container image includes and the wheel installs with
`osprey-ptz[mqtt]`. Configure it in the `mqtt` block of `osprey.json`; with
the container, edit
[your own copy of that file](installation.md#your-own-configuration-file).
The Home Assistant app and the NixOS module have no MQTT options yet.

```json
{
  "mqtt": {
    "enabled": true,
    "host": "mqtt.example.com",
    "port": 8883,
    "tls": true,
    "username": "osprey",
    "password": "change-me",
    "base_topic": "osprey",
    "device": "driveway"
  }
}
```

`tls` defaults to `false`, and `port` to 8883 with `tls` and 1883 without.
With `tls` on, the broker certificate is verified against the system CA store;
there is no option for a private CA yet. `base_topic` and `device` default to
`osprey`.

`mqtt.password` in `osprey.json` is the only way to give Osprey the broker
password; there is no environment variable or file option yet. Keep the file
mode `0600` and out of Git, and use a broker account that can only reach
Osprey's topics.

## Commands

Commands are JSON on QoS 1, under `<base_topic>/<device>/command/`:

| Topic | Example payload |
|---|---|
| `osprey/driveway/command/ptz/step` | `{"axis":"pan","direction":1,"step":4}` |
| `osprey/driveway/command/ptz/zoom` | `{"percent":50}` |
| `osprey/driveway/command/preset/goto` | `{"name":"gate"}` |
| `osprey/driveway/command/home` | `{}` |
| `osprey/driveway/command/sentry` | `{"camera":"AABBCCDDEEFF","enabled":true,"points":[{"name":"gate"}]}` |

Movement commands take the same movement lease as the patrol, and start a
manual hold like the console does. If the camera is
busy, the command fails with an error rather than forcing the move. Retained
commands are ignored, so a reconnect never replays a move.

The `sentry` command configures the [patrol](sentry.md). `camera` is a MAC or
display name. Every other field is optional, and a missing field keeps its
current value. It does not take the movement lease or start a manual hold,
because it changes configuration rather than moving the camera.

## State and events

| Topic | Contents |
|---|---|
| `osprey/driveway/event/control` | Result or error for each command. Not retained. |
| `osprey/driveway/state/ptz` | PTZ state and position, on change and at most once a second. |
| `osprey/driveway/state/sentry` | Patrol state. |

A `state/ptz` message looks like this (see [PTZ health](ptz-health.md)):

```json
{"camera":"g5-ptz-aabbccddeeff","state":"UNKNOWN","pan_tilt":"UNKNOWN",
 "zoom":"UNKNOWN","error":"motor state timeout","scene":"NIGHT","position":{},"at":0}
```

`scene` is `DAY` or `NIGHT`, as the camera last reported it, and `null` until
it does. A change of scene publishes a new message right away.

Payloads are validated: `step` is 1 to 20, `percent` is 0 to 100, and a
preset name is at most 64 characters. Movement commands carry no camera field,
so MQTT movement suits a controller with one camera. Only `sentry` names a
camera.
