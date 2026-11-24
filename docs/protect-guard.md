# Protect guard

A UniFi Protect console on the same network sees the camera through UniFi
discovery and lists it under **UniFi Devices** with an **Adopt** button. One
click takes the camera from Osprey: video and PTZ stop, and the camera has to
be pointed back at Osprey to recover.

The guard is a firewall rule that blocks the physical camera from reaching the
Protect console on the UniFi camera ports:

| Port | Protocol | Used for |
|---|---|---|
| 7442 | TCP | Control channel |
| 7444 | TCP | Snapshot upload |
| 7550 | TCP | Media |
| 10001 | UDP | UniFi discovery |

Osprey reaches the camera directly on the LAN, so the rule does not affect
it. The rule matches the camera's IP address, so reserve that address first.

This matters most when Protect also records Osprey's
[media persona](protect-dual-adoption.md). Protect then lists two entries for
one camera, and only one is safe to adopt.

## With `osprey protect-guard`

This works when Protect runs on a UniFi gateway (Cloud Gateway, UDM, UCG) that
uses the zone-based firewall.

1. In UniFi Network, open **Settings → Control Plane → Integrations** and
   create an API key. Save it to a file only you can read, such as
   `~/.config/osprey/unifi-api-key` with mode `0600`.
2. Reserve the camera's address: **Client Devices →** the camera **→ Settings
   → Fixed IP Address**.
3. Check, then create the rule if it is missing:

   ```sh
   osprey protect-guard --console https://192.0.2.1 --camera-ip 192.0.2.20 \
     --api-key-file ~/.config/osprey/unifi-api-key \
     --pin-file ~/.config/osprey/unifi-console.pin
   osprey protect-guard --console https://192.0.2.1 --camera-ip 192.0.2.20 \
     --api-key-file ~/.config/osprey/unifi-api-key \
     --pin-file ~/.config/osprey/unifi-console.pin --apply
   ```

`osprey` here is the command from the Python wheel or `./scripts/osprey` in a
source checkout. With only the container image, mount the key into a one-off
container:

```sh
docker run --rm --network host --user "$(id -u)" \
  -v ~/.config/osprey:/run/osprey \
  ghcr.io/grayslawson/osprey:0.9.0b7 protect-guard \
  --console https://192.0.2.1 --camera-ip 192.0.2.20 \
  --api-key-file /run/osprey/unifi-api-key \
  --pin-file /run/osprey/unifi-console.pin
```

Without `--apply` the command only reads. With it, the command creates one
policy named `Osprey guard: camera to Protect` and changes nothing else. Use
`--site` for a site other than `default`. `OSPREY_UNIFI_API_KEY_FILE` can
replace `--api-key-file`.

The command verifies the console's TLS certificate. UniFi consoles usually use
a self-signed one, so the first run fails until you choose how to trust it:

| Flag | Effect |
|---|---|
| `--pin-file PATH` | Trust on first use. Stores the certificate's SHA-256 in the file and requires the same certificate afterwards. |
| `--ca-file PATH` | Verify against a PEM CA or the console's certificate. |
| `--insecure` | Do not verify the certificate. Avoid it. |

Only one of the three can be used per run.

| Exit code | Meaning |
|---|---|
| 0 | The camera is guarded, or the rule was created. |
| 1 | No guard found. |
| 2 | Cannot tell, for example the API refused the key or the console has no zone-based firewall. |

`osprey doctor` runs the same check when the media persona is configured and
`OSPREY_UNIFI_API_KEY_FILE`, `OSPREY_UNIFI_CONSOLE` and `--camera` are set.
Osprey also logs a warning at startup while the media persona is on.

The API key can change your whole network. Keep it out of Osprey's
configuration, give it the narrowest role your console allows, and delete it
when you are done.

## By hand in UniFi Network

1. Reserve the camera's IP, as above.
2. Open **Settings → Policy Engine → Zones** (older releases: **Security →
   Firewall**) and select **Create Policy**:
   - Name: `Osprey guard: camera to Protect`
   - Action: **Block**
   - Source zone: **Internal**, source **IP**: the camera's address
   - Destination zone: **Gateway**, ports `7442, 7444, 7550, 10001`
   - Protocol: **TCP/UDP**, IP version **IPv4**
3. Save, then check that Osprey still shows live video.

## Protect on a separate NVR

A UNVR or CloudKey talks to the camera across the LAN, which the gateway does
not see, so a gateway policy cannot block it. Put the camera and the NVR on
different networks with a block rule between them, or use switch ACLs.

## Hand the camera back to Protect

Delete the policy, remove the camera from Osprey, then adopt it in Protect as
usual.
