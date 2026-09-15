# Security boundary

Osprey's ONVIF and RTSP services are designed to interoperate with ordinary
camera clients and are exposed on the local network. Treat them as camera
network services, not Internet services.

- Bind published ports to a dedicated LAN interface or localhost with
  `OSPREY_BIND`; do not publish them on `0.0.0.0` on an untrusted host.
- Permit only the camera, Frigate, and approved NVR clients through your host
  firewall or network segment.
- Protect the browser operator console with `OSPREY_ADMIN_PASSWORD_HASH` or
  `OSPREY_ADMIN_PASSWORD_FILE`, and put it behind an HTTPS reverse proxy when
  accessed remotely. UI sessions use CSRF protection; ONVIF/RTSP access does
  not grant access to the console.
- MQTT is disabled by default. When enabled, use TLS, certificate validation,
  broker authentication, and least-privilege ACLs. Osprey accepts only
  allow-listed command topics and ignores retained movement commands.
- Keep camera, Protect, MQTT, and admin secrets in root-owned files or a
  deployment secret manager outside the repository. Error logs must not contain
  their values.
- SOAP request bodies are capped at 256 KiB before XML parsing to limit memory
  and CPU abuse. Frigate URL validation rejects embedded credentials, queries,
  and fragments.

## ONVIF authentication

ONVIF WS-Security UsernameToken authentication is opt-in because some Frigate
clients use anonymous ONVIF. Set `OSPREY_ONVIF_USERNAME` together with either
`OSPREY_ONVIF_PASSWORD` or `OSPREY_ONVIF_PASSWORD_FILE` to require a
PasswordDigest on every SOAP request; there is no anonymous fallback when it is
enabled. Password files should be root-readable and contain only the password.
Digests expire after five minutes and each nonce is accepted once.

Leave the username unset for the anonymous compatibility mode, and test the
Frigate client's behavior before changing a running installation. A TLS
reverse proxy restricted to the Frigate host is another deployment option, but
test camera/NVR interoperability before inserting it into the camera path.

### What anonymous ONVIF exposes

With authentication disabled, any host that can reach Osprey's ONVIF port can
query device/media/PTZ capabilities, obtain stream and snapshot URIs, read
events, and issue PTZ operations (including moves and preset changes). The
ONVIF port does not grant the browser admin session, MQTT credentials, or the
camera's private control-channel credentials, but network access should still
be treated as control-plane access. Osprey's RTSP service is a separate local
media service. Set `OSPREY_RTSP_USERNAME` together with `OSPREY_RTSP_PASSWORD`
or `OSPREY_RTSP_PASSWORD_FILE` to require expiring-nonce RTSP Digest
authentication; when unset, RTSP remains anonymous for compatibility. Digest
authenticates the client but does not encrypt video, so use network ACLs or a
TLS-terminating tunnel when crossing an untrusted network. ONVIF authentication
does not automatically protect RTSP.

When authentication is enabled, clients must send an ONVIF WS-Security
UsernameToken using `PasswordDigest`; anonymous SOAP calls are rejected. Frigate
supports ONVIF credentials in each camera's `onvif.user` and `onvif.password`
settings and should be configured with the same Osprey credentials. This
protects PTZ and ONVIF metadata from other LAN clients, but clients/NVRs that do
not support WS-Security may no longer discover or control the camera.

Leave Frigate's `onvif.tls_insecure` at its default `false` when using Osprey
authentication. Frigate passes that setting to its ONVIF client as the
no-digest switch; setting it to `true` disables the UsernameToken digest and
will make an authenticated Osprey endpoint reject the requests.

**Recommendation:** keep anonymous ONVIF only on a firewall-isolated camera
network while validating a deployment. For a shared or untrusted LAN, create a
dedicated Osprey ONVIF account, configure the matching Frigate credentials,
test discovery, presets, movement, events, and autotracking, then enable
`OSPREY_ONVIF_USERNAME`/`OSPREY_ONVIF_PASSWORD_FILE`.

Implementation references: Frigate's [ONVIF camera configuration](https://github.com/blakeblackshear/frigate/blob/dev/frigate/config/camera/onvif.py),
[ONVIF controller](https://github.com/blakeblackshear/frigate/blob/dev/frigate/ptz/onvif.py),
and the [ONVIF Core Specification](https://www.onvif.org/specs/core/ONVIF-Core-Specification.html).

### RTSP authentication

Set `OSPREY_RTSP_USERNAME` together with `OSPREY_RTSP_PASSWORD`, or use
`OSPREY_RTSP_PASSWORD_FILE` for a file-backed secret. Osprey uses RTSP Digest
authentication. When unset, RTSP remains anonymous for compatibility. This is
independent of ONVIF authentication and does not encrypt video; use network
ACLs or a TLS-terminating tunnel as well.
