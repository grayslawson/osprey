# Security boundary

Osprey's ONVIF SOAP and RTSP protocols are intentionally compatible with
ordinary cameras and Frigate. They are not authenticated protocols in this
implementation: ONVIF clients commonly send empty credentials, and enabling
WS-Security by default would break existing Frigate deployments. Treat these
ports as a camera-network service, not an Internet service.

* Bind published ports to a dedicated LAN interface or localhost (`OSPREY_BIND`);
  do not publish them to `0.0.0.0` on an untrusted host. Permit only the camera
  and Frigate in the host firewall or an equivalent network segment.
* Put the operator UI behind HTTPS (a reverse proxy is recommended) and set
  `OSPREY_ADMIN_PASSWORD_HASH` or `OSPREY_ADMIN_PASSWORD_FILE`. The UI session
  has CSRF protection; ONVIF/RTSP access does not imply UI access.
* MQTT is disabled by default. When enabled, use broker ACLs, credentials and
  TLS certificate verification. Movement commands are allow-listed and retained
  commands are ignored; never grant the bridge a wildcard broker ACL.
* Keep Protect credentials and password files outside the repository, with
  root-only permissions. Configuration and error logs must not contain them.

SOAP request bodies are capped at 256 KiB before XML parsing to limit memory and
CPU abuse. Frigate URL metadata rejects credentials, queries and fragments; it
is not fetched by Osprey and must still be treated as operator-supplied data.

If authenticated ONVIF becomes necessary, deploy an authenticating reverse
proxy restricted to the Frigate host and configure Frigate with those proxy
credentials. Do not expose a proxy to the camera callback path without testing
the camera's ONVIF interoperability; this remains an explicit deployment
choice rather than an unsafe compatibility-breaking default.
