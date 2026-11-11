#!/usr/bin/env bash
# manage.py with the wire package on the path — point a camera at this controller.
set -euo pipefail
cd "$(dirname "$0")"
if [ -d ../pyunifiwire/src ]; then
  wire_path="$(cd ../pyunifiwire/src && pwd)"
  export PYTHONPATH="${wire_path}${PYTHONPATH:+:$PYTHONPATH}"
fi
exec python3 manage.py "$@"
