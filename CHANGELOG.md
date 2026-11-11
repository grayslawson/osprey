# Changelog

All notable changes to Osprey are listed here. Entries are generated from
Conventional Commit subjects by git-cliff (see cliff.toml).

## Unreleased (0.9.0b1)

First beta. G5 PTZ fully supported; other UniFi PTZ models experimental.

### Features
- **protect-guard:** `osprey protect-guard --check/--apply` checks for or
  creates the UniFi firewall policy that keeps Protect from adopting the
  physical camera; `osprey doctor` and startup warn when the read-only ONVIF
  persona is on. See docs/protect-guard.md.

### Packaging
- Version 0.9.0b1. Release workflow accepts PEP 440 pre-release tags and
  publishes them as pre-releases (image tags `0.9.0b1` and `v0.9.0b1`; `latest`
  moves only on final releases).
- Issue forms: bug report and camera model support.

### Features
- **ptz:** Manual wins, then resume. A person's PTZ command (console, MQTT, a
  non-autotrack ONVIF user such as Protect) overrides Frigate and holds it off for
  `ptz.manual_hold_sec` (default 30 s). New `ptz.autotrack_users` setting.
- **probe:** `osprey probe` writes a redacted, read-only data bundle for an
  unsupported UniFi PTZ model. Models other than the G5 PTZ are marked
  experimental (log warning, `support` in the operator API).
- **onvif:** `onvif.log_calls` logs every SOAP call with user, peer and PTZ source.

### Renamed: cuckoo to osprey
- The controller source moved from `cuckoo/` to `osprey/`. Deployments that
  mount or run `cuckoo/main.py` directly must switch to `osprey/main.py`. The
  release image, Home Assistant add-on, NixOS package and PyPI wheel are updated.
- Default file names are now `osprey.json` and `osprey.pem`. An existing
  `cuckoo.json` or `cuckoo.pem` is still used when the new file does not exist,
  so an upgraded camera keeps trusting its certificate.
- Logger names changed from `cuckoo.*` to `osprey.*`. Update any log filters.
- `handoff.sh` reads `OSPREY_*` variables and falls back to the old `CUCKOO_*` names.
- The WS-Discovery device UUID is unchanged, so NVRs keep the same device.

### Bug fixes
- **onvif:** Fail closed on partial authentication config
- Bound RTSP digest nonce state
- Tighten RTSP digest validation
- Repair package manifests and validation workflow
- Bound ONVIF authentication replay state
- Close completed setup endpoint
- **adoption:** Disarm every track instead of only the last
- **proxmox:** Generate valid host-network compose with camera ports

### Documentation
- Normalize Protect dual-adoption note
- Clarify published image onboarding
- Document Home Assistant Frigate deployment
- Clarify separate RTSP authentication
- Add package installation commands
- Clarify package publication boundaries
- **license:** Unify copyright notices and add NOTICE

### Features
- Initial commit of cuckoo surveillance and automation project
- Establish Osprey PTZ controller
- Add container-based package integrations
- Add optional RTSP digest authentication
- Add first-run setup and secure media packaging
- **proxmox:** Add pending community-scripts LXC ct/install pair

### Packaging
- **docker:** Runnable hardened image and host-network release compose

