#!/usr/bin/env bash
# custody.py with the wire package on the path — move a camera between controllers.
set -euo pipefail
cd "$(dirname "$0")"
if [ -d ../pyunifiwire/src ]; then
  wire_path="$(cd ../pyunifiwire/src && pwd)"
  export PYTHONPATH="${wire_path}${PYTHONPATH:+:$PYTHONPATH}"
fi
exec python3 custody.py "$@"
