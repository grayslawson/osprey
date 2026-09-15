# Osprey contract tests

The setup tests exercise the browser first-run contract described in
[`specs/001-release-onboarding/contracts/setup-http.md`](../../specs/001-release-onboarding/contracts/setup-http.md).
The doctor tests execute the read-only command contract in
[`specs/001-release-onboarding/contracts/doctor-cli.md`](../../specs/001-release-onboarding/contracts/doctor-cli.md).

Run the focused suites with:

```sh
uv run --with pytest pytest cuckoo/tests/test_setup_wizard.py cuckoo/tests/test_doctor.py -q
```
