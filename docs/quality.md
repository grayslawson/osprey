# Osprey quality map

This ledger records what each domain's tests prove. It does not claim that
every integration is production-ready. Update a row when code, tests or
supported installations change.

| Domain | Primary code | Evidence | Current bar |
| --- | --- | --- | --- |
| Camera control and adoption | `osprey/controller.py`, `osprey/adoption.py`, `osprey/ptz.py` | `osprey/tests/test_adoption_flow.py`, `osprey/tests/test_layers.py` | Unit and fake-peer coverage; hardware validation is opt-in |
| ONVIF and discovery | `osprey/onvif.py`, `osprey/discovery.py` | `osprey/tests/test_onvif*.py`, `osprey/tests/test_discovery.py` | Contract tests for profiles, PTZ, auth, and discovery |
| Media and RTSP | `osprey/media.py`, `osprey/rtsp.py`, `osprey/snapshots.py` | `osprey/tests/test_rtsp.py`, `osprey/tests/test_rtsp_session.py`, `osprey/tests/test_media.py`, `osprey/tests/test_snapshots.py` | Codec and authentication behavior covered without hardware |
| Administration and setup | `osprey/setup_wizard.py`, `osprey/main.py` | `osprey/tests/test_setup_wizard.py`, `osprey/tests/test_stack.py` | Safe generated state, error paths, and restart behavior |
| Distribution | `docker/`, `homeassistant/`, `nix/`, `packaging/` | `homeassistant/validate.sh`, package workflow, Compose checks | Metadata and build inputs validated in CI |
| Patrol and media persona | `osprey/sentry*.py`, `osprey/identity.py`, `osprey/discovery.py` | `osprey/tests/test_sentry*.py`, `osprey/tests/test_media_persona.py` | Fake-peer coverage; no hardware run in CI |
| Security and operations | `SECURITY.md`, `scripts/osprey-doctor.sh` | `scripts/validate-repo.sh`, Trivy workflow, doctor tests | Explicit trust boundaries; deployment-specific verification remains required |

## Updating the ledger

When a row’s bar changes, include the reason, verification command, and any
remaining limitation in the same pull request. Do not mark a domain complete
solely because a test exists; record the boundary and what the test does not
cover. Keep credentials, private addresses, and production observations out of
this file.
