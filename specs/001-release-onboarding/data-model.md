# Data Model: Release Onboarding and Diagnostics

## Deployment Configuration

Represents non-secret runtime settings written by setup.

| Field | Required | Rules |
|---|---:|---|
| advertised host | yes | IP address or DNS hostname; never a wildcard |
| bind address | yes | IP address; wildcard allowed only when explicitly selected |
| camera name | yes | 1–64 non-empty characters |
| camera MAC | yes | exactly 12 hexadecimal digits after separator normalization |
| camera IP | yes | valid IP address |
| Frigate URL | yes | validated HTTP(S) base URL without embedded credentials |
| ports and tracks | generated/defaulted | must pass runtime configuration validation |

State transitions: `unconfigured → validated → committed → active after restart`. A failed
validation or interrupted write remains `unconfigured` or preserves the last committed state.
Once committed, setup cannot silently transition back to `unconfigured`.

## Deployment Secret

Represents an administrator hash or protocol password stored separately from runtime JSON.
Secrets are generated or supplied through protected files, written with mode `0600`, returned
only through the one-time setup response where necessary, and never included in logs or doctor
output. Secret rotation creates a new committed value without changing the public contract.

## Diagnostic Check

| Field | Meaning |
|---|---|
| name | stable check identifier shown to operators |
| status | `ok`, `warning`, or `error` |
| evidence | non-secret observed condition |
| remediation | concrete next action when status is not `ok` |
| required | whether failure affects command success |

Each check runs independently. Required errors produce a non-zero command status; warnings do
not. A missing optional tool is a warning when the associated capability is not required.

## Protocol Exposure

Represents a listener and its client-facing authentication mode. ONVIF and RTSP records are
independent and include listener, purpose, mode (`anonymous` or `authenticated`), and exposure
guidance. Passwords are never model fields in operator-visible status.
