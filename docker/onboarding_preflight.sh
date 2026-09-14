#!/usr/bin/env bash
# Read-only checks for a local/test Osprey checkout.  This deliberately does
# not inspect or contact a camera, Frigate, Protect, or a secrets store.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
config_file=${1:-cuckoo.json}
fail() { printf 'onboarding preflight: %s\n' "$*" >&2; exit 1; }

[[ -f cuckoo/run.sh ]] || fail "not an Osprey checkout (cuckoo/run.sh is missing)"
[[ -f cuckoo/main.py ]] || fail "checkout is incomplete (cuckoo/main.py is missing)"
command -v python3 >/dev/null || fail "python3 is required"

if [[ -e "$config_file" ]]; then
  python3 - "$config_file" <<'PY' || exit $?
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
try:
    value = json.loads(p.read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"{p}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
if not isinstance(value, dict):
    raise SystemExit(f"{p}: top-level value must be an object")
for key in ("password", "token", "secret", "private_key"):
    if key in value:
        raise SystemExit(f"{p}: do not put credentials in config; use deployment secrets")
print(f"validated config: {p}")
PY
else
  [[ "$config_file" == cuckoo.json ]] || fail "config file not found: $config_file"
  printf 'onboarding preflight: no cuckoo.json (defaults will be used)\n'
fi

printf 'onboarding preflight: checkout %s is ready for LOCAL/TEST use\n' "$(git rev-parse --show-toplevel 2>/dev/null || printf unknown)"
printf 'onboarding preflight: no production services, cameras, Frigate, or credentials were contacted\n'
