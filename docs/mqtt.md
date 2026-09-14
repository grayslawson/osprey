# MQTT automation

MQTT is optional and disabled by default. When enabled, Osprey connects to the
broker configured in `cuckoo.json`; it does not expose a broker itself.

```json
{
  "mqtt": {
    "enabled": true,
    "host": "mqtt.local",
    "port": 8883,
    "tls": true,
    "username": "osprey",
    "password": "load-this-from-a-secret-at-deployment",
    "base_topic": "osprey",
    "device": "driveway"
  }
}
```

Put credentials in a root-owned configuration or deployment secret, not in a
repository. TLS verifies broker certificates when `tls` is enabled. Configure
broker ACLs so only trusted publishers can write command topics.

With the example above, publish JSON commands to these QoS 1 topics:

| Topic | Payload |
| --- | --- |
| `osprey/driveway/command/ptz/step` | `{"axis":"pan","direction":1,"step":4}` |
| `osprey/driveway/command/ptz/zoom` | `{"percent":50}` |
| `osprey/driveway/command/preset/goto` | `{"name":"gate"}` |
| `osprey/driveway/command/home` | `{}` |

Osprey publishes a structured, non-retained result or error to
`osprey/driveway/event/control`. Retained command messages are deliberately
ignored, so reconnecting never replays a camera movement. MQTT commands enter
the same movement gate as the console, sentry, and tracking producers; a
contended movement is reported as an error instead of bypassing camera safety.
