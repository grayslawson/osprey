# Doctor CLI Contract

Command: `osprey doctor [options]`

Supported options include `--host`, `--camera`, `--callback`, `--frigate`, `--config`,
`--ports`, and `--help`. Values may also be supplied through the documented environment
variables. `--callback` identifies the Osprey callback URL that a camera or external service
must be able to reach; it is checked without sending control commands.

## Output

Each check emits one line beginning with exactly one of:

- `OK` for a passing check;
- `WARNING` for an unavailable optional capability or an inconclusive external dependency;
- `ERROR` for invalid configuration or a required failed check.

Lines contain evidence and remediation-oriented context but never secret values.

## Exit status

- `0`: no required check failed;
- `1`: one or more required checks failed;
- `2`: command-line usage error.

The command is read-only: it MUST NOT change camera state, firewall rules, Compose files,
Frigate state, or Osprey configuration.
