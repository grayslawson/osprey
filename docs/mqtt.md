# MQTT automation

MQTT is optional and disabled by default. When enabled, Osprey connects to the
configured broker; it does not run or expose a broker of its own.

```json
{
  "mqtt": {
    "enabled": true,
    "host": "mqtt.example",
    "port": 8883,
    "tls": true,
    "username": "osprey",
    "password": "read-from-your-secret-manager",
    "base_topic": "osprey",
    "device": "driveway"
  }
}
```

Put credentials in a root-owned deployment secret, not in `cuckoo.json` or
documentation. TLS verifies broker certificates when `tls` is enabled. Use
broker ACLs so only trusted publishers can write Osprey command topics.

Commands are JSON and use QoS 1:

| Topic | Payload |
| --- | --- |
| `osprey/driveway/command/ptz/step` | `{"axis":"pan","direction":1,"step":4}` |
| `osprey/driveway/command/ptz/zoom` | `{"percent":50}` |
| `osprey/driveway/command/preset/goto` | `{"name":"gate"}` |
| `osprey/driveway/command/home` | `{}` |

Osprey publishes a structured, non-retained result or error on
`osprey/driveway/event/control`. Retained command messages are deliberately
ignored so reconnecting never replays camera movement. MQTT commands enter the
same movement gate as console, sentry, and tracking producers; contention is
reported as an error instead of bypassing camera safety.

For multiple cameras, give each camera a distinct `device` value and follow the
[multi-camera schema](multi-camera.md).
