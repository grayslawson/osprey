# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.9.x (beta) | Yes, latest pre-release only |
| < 0.9 | No |

## Reporting a vulnerability

Report suspected vulnerabilities through GitHub: **Security → Advisories →
Report a vulnerability**. Do not open a public issue for an exploitable
problem.

Include the Osprey version, how it is deployed, a minimal reproduction and the
impact. Remove credentials, private addresses, MAC addresses and recordings
from anything you attach.

## Deployment expectations

Osprey's control, media, ONVIF and RTSP ports belong on a trusted LAN or VLAN.
The console stays locked until an admin password is set. ONVIF and RTSP
authentication are opt-in; enable ONVIF authentication once you have tested it
with Frigate. [`docs/security.md`](docs/security.md) has the details.
