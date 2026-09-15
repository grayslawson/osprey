# Osprey quality map

This is a lightweight, reviewable quality ledger—not a claim that every
integration is production-ready. Update a row when code, tests, or supported
installations change. Use the linked evidence instead of relying on memory.

| Domain | Primary code | Evidence | Current bar |
| --- | --- | --- | --- |
| Camera control and adoption | `cuckoo/controller.py`, `cuckoo/adoption.py`, `cuckoo/ptz.py` | `cuckoo/tests/test_adoption_flow.py`, `cuckoo/tests/test_layers.py` | Unit and fake-peer coverage; hardware validation is opt-in |
| ONVIF and discovery | `cuckoo/onvif.py`, `cuckoo/discovery.py` | `cuckoo/tests/test_onvif*.py`, `cuckoo/tests/test_discovery.py` | Contract tests for profiles, PTZ, auth, and discovery |
| Media and RTSP | `cuckoo/media.py`, `cuckoo/rtsp.py`, `cuckoo/snapshots.py` | `cuckoo/tests/test_rtsp.py`, `cuckoo/tests/test_media.py`, `cuckoo/tests/test_snapshots.py` | Codec and authentication behavior covered without hardware |
| Administration and setup | `cuckoo/setup_wizard.py`, `cuckoo/main.py` | `cuckoo/tests/test_setup_wizard.py`, `cuckoo/tests/test_stack.py` | Safe generated state, error paths, and restart behavior |
| Distribution | `docker/`, `homeassistant/`, `nix/`, `packaging/` | `homeassistant/validate.sh`, package workflow, Compose checks | Metadata and build inputs validated in CI |
| Security and operations | `SECURITY.md`, `scripts/osprey-doctor.sh` | `scripts/validate-repo.sh`, Trivy workflow, doctor tests | Explicit trust boundaries; deployment-specific verification remains required |

## Updating the ledger

When a row’s bar changes, include the reason, verification command, and any
remaining limitation in the same pull request. Do not mark a domain complete
solely because a test exists; record the boundary and what the test does not
cover. Keep credentials, private addresses, and production observations out of
this file.
