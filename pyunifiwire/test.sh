#!/bin/bash
# The package's own checks. Needs nothing installed and no network.
set -euo pipefail
cd "$(dirname "$0")"
echo "== mypy =="
python3 -m mypy . 2>&1 | tail -20
echo "== pytest =="
python3 -m pytest 2>&1 | tail -20
