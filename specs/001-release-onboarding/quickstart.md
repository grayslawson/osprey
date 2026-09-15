# Quickstart Validation

These checks validate the feature against a local, isolated Osprey checkout. They do not
require a physical camera or a Frigate installation for the unit-level scenarios.

## Prerequisites

- Python 3.11 or newer and the repository test dependencies;
- a temporary writable state directory;
- `curl` for HTTP checks; Docker or Podman is optional.

## Scenario 1: First-run artifacts

1. Start Osprey in setup mode with a temporary configuration path.
2. Open the setup status route and confirm `pending` is true.
3. Submit valid neutral values from [setup-http.md](contracts/setup-http.md).
4. Confirm the JSON configuration contains no passwords, generated secret files have mode
   `0600`, and the response reports restart required.
5. Submit a second save and confirm it is rejected without changing file timestamps or content.

Automated coverage: `cuckoo/tests/test_setup_wizard.py`.

## Scenario 2: Doctor success and failure isolation

1. Run `scripts/osprey-doctor.sh --host 192.0.2.10 --config cuckoo.json` in a controlled
   environment.
2. Run with `--callback http://192.0.2.10:8000` and confirm each applicable result uses the
   documented prefix and a healthy run exits `0`.
3. Repeat with an invalid host, invalid config, unreachable camera route, and unavailable
   Frigate URL; confirm failures are reported independently and exit status is non-zero only
   for required errors.

Automated coverage: shell syntax checks plus the repository configuration and integration tests.

## Scenario 3: Protocol exposure review

1. Start with generated defaults and inspect status/log output for mode names only.
2. Confirm anonymous ONVIF remains available and its isolation guidance is documented.
3. Enable ONVIF UsernameToken and RTSP Digest credentials in an isolated test configuration.
4. Confirm valid credentials succeed, invalid credentials fail, stale challenges expire, and
   no password appears in logs or diagnostic output.

Automated coverage: `cuckoo/tests/test_onvif_auth.py` and `cuckoo/tests/test_rtsp.py`.

## Full validation

Run the repository's documented test script, mypy checks, shell syntax checks, compose
validation, package checks, and Home Assistant metadata validation before release.

## Validation record

On 2026-09-15, the focused setup/doctor suites and the complete `cuckoo/tests` suite passed
under an ephemeral `uv` pytest environment. Shell syntax checks passed for the doctor,
update, Proxmox, and test scripts. Mypy passed for application sources when test and harness
modules are excluded; the repository's legacy harness imports require the optional ONVIF
client package and remain outside the source check.
