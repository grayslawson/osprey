# Changelog

All notable changes to Osprey are listed here. Entries are generated from
Conventional Commit subjects by git-cliff (see cliff.toml).

## Unreleased

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

