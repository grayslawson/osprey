# Keep UniFi Protect off the physical camera

Osprey is the camera's only controller. A UniFi Protect console on the same
network still sees the camera through UniFi discovery and lists it under
**UniFi Devices** with an **Adopt** button. One click on it takes the camera
away from Osprey: video stops, PTZ stops, and getting it back means
re-pointing the camera at Osprey.

This matters most when you also add Osprey to Protect as a third-party ONVIF
camera ([Protect dual adoption](protect-dual-adoption.md)). Then Protect lists
two entries for the same camera, and only one of them is safe to adopt.

## The guard

Block the physical camera from talking to the Protect console on the UniFi
camera ports:

| Port | Protocol | Used by |
|---|---|---|
| 7442 | TCP | camera control channel |
| 7444 | TCP | snapshot upload |
| 7550 | TCP | media ingest |
| 10001 | UDP | UniFi discovery |

Osprey talks to the camera directly on the LAN, so the guard does not affect
it. It only works if the camera's IP never changes, so reserve it first.

### With `osprey protect-guard` (UniFi gateway consoles)

This works when Protect runs on the UniFi gateway itself (Cloud Gateway, UDM,
UCG) with the zone-based firewall.

1. In UniFi Network, open **Settings > Control Plane > Integrations** and create
   an API key. Save it to a file readable only by you, for example
   `~/.config/osprey/unifi-api-key` with mode `0600`.
2. Reserve the camera's address: **Client Devices >** the camera **> Settings >
   Fixed IP Address**.
3. Check, then apply:

   ```sh
   osprey protect-guard --console https://192.168.1.1 --camera-ip 192.0.2.20 \
     --api-key-file ~/.config/osprey/unifi-api-key            # check only
   osprey protect-guard ... --apply                           # create it if missing
   ```

   `--check` (the default) only reads. `--apply` creates one policy, named
   "Osprey guard: camera to Protect". Nothing else is changed. The console
   certificate is self-signed, so verification is off unless you pass
   `--verify-tls`.

The exit code is 0 when the camera is guarded, 1 when no guard is found, and 2
when it cannot tell (for example, a console without the zone-based firewall).
`osprey doctor` runs the same check when `OSPREY_UNIFI_API_KEY_FILE`,
`OSPREY_UNIFI_CONSOLE` and `--camera` are set. Osprey also logs a warning at
startup whenever the read-only ONVIF persona is on.

The API key can change your whole network. Keep it out of the Osprey
configuration, delete it after use, or give it the narrowest role your console
allows.

### By hand in UniFi Network

1. Reserve the camera's IP, as above.
2. **Settings > Policy Engine > Zones** (older releases: **Security >
   Firewall**) **> Create Policy**:
   - Name: `Osprey guard: camera to Protect`
   - Action: **Block**
   - Source zone: **Internal**, Source: **IP**, the camera's address
   - Destination zone: **Gateway**, Port: `7442, 7444, 7550, 10001`
   - Protocol: **TCP/UDP**, IP version **IPv4**
3. Save. Then check that Osprey still shows live video.

### Protect on a separate NVR (UNVR, CloudKey)

The gateway does not see traffic between two LAN devices, so a gateway policy
cannot help. Put the camera and the NVR in different networks with a block
rule between them, or use switch ACLs if your switches support them.

## To hand the camera back to Protect

Delete the policy, remove the camera from Osprey, then adopt it in Protect as
usual.
