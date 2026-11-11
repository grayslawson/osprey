# Changelog

All notable changes to Osprey are listed here. Entries are generated from
Conventional Commit subjects by git-cliff (see cliff.toml).

## Unreleased

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

