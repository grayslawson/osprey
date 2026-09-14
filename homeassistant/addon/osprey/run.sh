#!/bin/sh
set -eu

# Supervisor stores validated add-on options here.  Python is used instead of
# jq/bashio so the image remains usable with both the HA builder and root builds.
OPTIONS=/data/options.json
if [ ! -r "$OPTIONS" ]; then
  echo "Osprey: /data/options.json is missing" >&2
  exit 1
fi

get_option() {
  python3 - "$OPTIONS" "$1" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    value = json.load(f).get(sys.argv[2], "")
if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

HOST=$(get_option advertised_host)
if [ -z "$HOST" ]; then
  echo "Osprey: set advertised_host to the host/LAN address reachable by the camera" >&2
  exit 1
fi

set -- python3 /opt/osprey/cuckoo/main.py \
  --host "$HOST" \
  --cert /data/osprey/cuckoo.pem \
  --name "$(get_option camera_name)" \
  --tracks "$(get_option tracks)" \
  --onvif-port "$(get_option onvif_port)" \
  --rtsp-port "$(get_option rtsp_port)" \
  --control-port 7442 --ingest-port 7550 --snapshot-port 7444 \
  --dump /data/osprey/messages.jsonl

CAMERA_IP=$(get_option camera_ip)
CAMERA_MAC=$(get_option camera_mac)
[ -n "$CAMERA_IP" ] && set -- "$@" --expected-camera-ip "$CAMERA_IP"
[ -n "$CAMERA_MAC" ] && set -- "$@" --expected-camera-mac "$CAMERA_MAC"
[ "$(get_option announce)" = "false" ] && set -- "$@" --no-announce
exec "$@"
