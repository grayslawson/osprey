# 005 Easy setup: tasks

- [x] Zero-config start: optional `OSPREY_HOST`/`OSPREY_IMAGE`/`OSPREY_SETUP`,
      LAN address detection, automatic first-run wizard, link banner, token
      expiry, restart after save, Home Assistant without `advertised_host`.
- [x] Camera discovery and adoption in the wizard; `osprey adopt` in the image.
- [x] ONVIF user `frigate` for new installs; Frigate block; Add to Frigate.
- [ ] Health-check step.
- [ ] README quick start and installation guide.

## Verification

- Unit: `osprey/tests/test_setup_wizard.py`, `test_config.py`,
  `test_main_startup.py`.
- End to end: the wizard in a container on an isolated network against the
  fake camera and fake Frigate in `osprey/harness/`.
