#!/bin/bash
# Run finch with the wire package on the path.
# In development pyunifiwire sits beside us; installed, this line is unnecessary.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$(cd ../pyunifiwire/src && pwd)${PYTHONPATH:+:$PYTHONPATH}"
exec python3 main.py "$@"
