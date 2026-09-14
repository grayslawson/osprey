#!/usr/bin/env bash
# Run cuckoo with the wire package on the path.
# In development pyunifiwire sits beside us; installed, this line is unnecessary.
set -euo pipefail
cd "$(dirname "$0")"
if [ -d ../pyunifiwire/src ]; then
  wire_path="$(cd ../pyunifiwire/src && pwd)"
  export PYTHONPATH="${wire_path}${PYTHONPATH:+:$PYTHONPATH}"
fi
exec python3 main.py "$@"
