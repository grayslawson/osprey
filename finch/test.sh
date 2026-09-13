#!/bin/bash
# Everything that runs without a controller. Run after every edit.
# The wire lives in pyunifiwire; cuckoo is optional and only used by the
# cross-check tests, which skip themselves when it is not there.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$(cd ../pyunifiwire/src && pwd)${PYTHONPATH:+:$PYTHONPATH}"
[ -d ../cuckoo ] && export PYTHONPATH="$PYTHONPATH:$(cd ../cuckoo && pwd)"
echo "== mypy =="
python3 -m mypy . 2>&1 | tail -20
echo "== pytest =="
python3 -m pytest 2>&1 | tail -20
