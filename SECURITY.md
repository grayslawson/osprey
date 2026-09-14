# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities through the repository's GitHub
**Security → Advisories → Report a vulnerability** flow. Do not open a public
issue for an exploitable problem or include camera credentials, private IP
addresses, MAC addresses, or recordings in a report.

Include the affected Osprey version, deployment method, a minimal
reproduction, and the impact. Remove secrets and identifying network details
from logs before attaching them.

## Deployment expectations

Osprey exposes camera-facing control, media, and ONVIF/RTSP services on the
local network. Keep those ports on a trusted LAN or VLAN, configure the
browser admin password before exposing the operator console, and enable ONVIF
WS-Security only after verifying Frigate interoperability. See
[`docs/security.md`](docs/security.md) for the complete deployment guidance.
