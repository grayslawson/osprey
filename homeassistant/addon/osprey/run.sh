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

# Supervisor mounts /data root-owned at run time, so the state directory is
# made here, as root, and the controller then runs as the unprivileged user.
install -d -o osprey -g osprey -m 0750 /data/osprey

# The sidebar console reaches Osprey through the Supervisor's ingress proxy,
# which has this fixed address; Home Assistant has already signed that user in.
export OSPREY_TRUSTED_INGRESS_PEER=172.30.32.2

# Optional password for the console on the direct port (the Web UI link).
PASSWORD=$(get_option admin_password)
if [ -n "$PASSWORD" ]; then
  umask 077
  printf '%s' "$PASSWORD" > /data/osprey/admin-password
  chown osprey:osprey /data/osprey/admin-password
  export OSPREY_ADMIN_PASSWORD_FILE=/data/osprey/admin-password
else
  rm -f /data/osprey/admin-password
fi
unset PASSWORD

# ONVIF and RTSP credentials. Left empty, a new install generates and logs them
# once (stored in /data/osprey); an existing install stays anonymous until set.
for SERVICE in onvif rtsp; do
  SECRET=$(get_option "${SERVICE}_password")
  USER_NAME=$(get_option "${SERVICE}_username")
  if [ -n "$SECRET" ]; then
    (umask 077; printf '%s' "$SECRET" > "/data/osprey/${SERVICE}-password")
    chown osprey:osprey "/data/osprey/${SERVICE}-password"
    export "OSPREY_$(echo "$SERVICE" | tr a-z A-Z)_USERNAME=${USER_NAME:-osprey}"
    export "OSPREY_$(echo "$SERVICE" | tr a-z A-Z)_PASSWORD_FILE=/data/osprey/${SERVICE}-password"
  fi
done
unset SECRET
# auto: new installs are secure, existing ones keep their behaviour.
[ "$(get_option security)" = "auto" ] || export OSPREY_SECURITY="$(get_option security)"

# Upgrade path: add-on builds before the rename kept the control certificate as
# cuckoo.pem. The camera trusts that certificate, so keep using it.
CERT=/data/osprey/osprey.pem
if [ ! -e "$CERT" ] && [ -e /data/osprey/cuckoo.pem ]; then
  CERT=/data/osprey/cuckoo.pem
fi

set -- /usr/bin/setpriv --reuid=osprey --regid=osprey --init-groups \
  python3 /opt/osprey/osprey/main.py \
  --host "$HOST" \
  --cert "$CERT" \
  --name "$(get_option camera_name)" \
  --tracks "$(get_option tracks)" \
  --onvif-port "$(get_option onvif_port)" \
  --rtsp-port "$(get_option rtsp_port)" \
  --control-port 7442 --ingest-port 7550 --snapshot-port 7444

CAMERA_IP=$(get_option camera_ip)
CAMERA_MAC=$(get_option camera_mac)
[ -n "$CAMERA_IP" ] && set -- "$@" --expected-camera-ip "$CAMERA_IP"
[ -n "$CAMERA_MAC" ] && set -- "$@" --expected-camera-mac "$CAMERA_MAC"
[ "$(get_option announce)" = "false" ] && set -- "$@" --no-announce
exec "$@"
