#!/usr/bin/env bash
# Hand the camera over to osprey, and hand it back.
#
# The camera already dials this host on :7442, so nothing on the camera has to
# change: free that one port, bind it ourselves, and the camera reconnects on its
# own retry. Our hello reply carries a null controllerUuid with overrideUuid set,
# which is what lets us take over a camera already adopted elsewhere without
# resetting it.
#
# Only :7442 is contested. The ingest and snapshot ports are ours to choose —
# the camera is told where to push — so they are moved out of the way of the
# resident stack (which holds :7550 and :7444) rather than fought over.
#
#   ./handoff.sh take            # stop the resident controller, run osprey
#   ./handoff.sh give-back       # stop osprey, restore the resident controller
#   ./handoff.sh status
#
# Taking the socket is not enough on its own. A camera that already believes it is
# adopted sends nothing but timeSync, ignores a hello it did not ask for, and
# answers settings with "Unauthorized" — it only introduces itself when it thinks
# nobody owns it. So `take` first asks the resident controller to release it, and
# `give-back` asks for it to be taken again. Set the credentials for that with
#
#   export OSPREY_USER=admin OSPREY_PASSWORD=…
#
# or point OSPREY_SECRETS at a directory holding `protect-admin-user` and
# `protect-admin-password`. Without either, the ports still move but the camera
# will sit there refusing orders, and `take` says so.
#
# Ctrl-C during `take` restores the resident controller before exiting.

set -euo pipefail
cd "$(dirname "$0")"

CONTAINER="${OSPREY_CONTAINER:-${CUCKOO_CONTAINER:-osprey-resident}}"
RESIDENT_UNIT="${OSPREY_RESIDENT_UNIT:-${CUCKOO_RESIDENT_UNIT:-osprey}}"
HOST="${OSPREY_HOST:-${CUCKOO_HOST:-}}"
LOG="${OSPREY_LOG:-${CUCKOO_LOG:-/tmp/osprey.log}}"
DUMP="${OSPREY_DUMP:-${CUCKOO_DUMP:-/tmp/osprey-messages.jsonl}}"
INGEST_PORT="${OSPREY_INGEST_PORT:-${CUCKOO_INGEST_PORT:-17550}}"
SNAPSHOT_PORT="${OSPREY_SNAPSHOT_PORT:-${CUCKOO_SNAPSHOT_PORT:-17444}}"
RTSP_PORT="${OSPREY_RTSP_PORT:-${CUCKOO_RTSP_PORT:-8554}}"
ONVIF_PORT="${OSPREY_ONVIF_PORT:-${CUCKOO_ONVIF_PORT:-8000}}"
TRACKS="${OSPREY_TRACKS:-${CUCKOO_TRACKS:-video1:h264,video2:h265,video3:h264}}"  # full-res H.264 + low H.264 for ONVIF, H.265 sub for efficiency; override freely
SECRETS="${OSPREY_SECRETS:-${CUCKOO_SECRETS:-$HOME/.osprey/secrets}}"
if [[ -z "${OSPREY_SECRETS:-}${CUCKOO_SECRETS:-}" && ! -d "$SECRETS" && -d "$HOME/.cuckoo/secrets" ]]; then
  SECRETS="$HOME/.cuckoo/secrets"  # pre-rename default
fi
CAMERA_MAC="${OSPREY_CAMERA_MAC:-${CUCKOO_CAMERA_MAC:-}}"
CAMERA_HOST="${OSPREY_CAMERA_HOST:-${CUCKOO_CAMERA_HOST:-}}"
CAMERA_USER="${OSPREY_CAMERA_USER:-${CUCKOO_CAMERA_USER:-}}"
CAMERA_PASSWORD="${OSPREY_CAMERA_PASSWORD:-${CUCKOO_CAMERA_PASSWORD:-}}"
CAMERA_TOKEN="${OSPREY_CAMERA_TOKEN:-${CUCKOO_CAMERA_TOKEN:-osprey}}"
# A per-camera credential file in the secrets store, outside the project tree so it
# is never committed: {"host": …, "user": …, "password": …}.
CAMERA_SECRET="${OSPREY_CAMERA_SECRET:-${CUCKOO_CAMERA_SECRET:-$SECRETS/camera-$CAMERA_MAC.json}}"
PIDFILE=/tmp/osprey.pid

have_container() { sudo docker inspect "$CONTAINER" >/dev/null 2>&1; }

credentials() {
  # Explicit environment wins; otherwise fall back to files if they are readable.
  local user="${OSPREY_USER:-${CUCKOO_USER:-}}" password="${OSPREY_PASSWORD:-${CUCKOO_PASSWORD:-}}"
  if [[ -n "$user" && -n "$password" ]]; then
    printf '%s\n%s\n' "$user" "$password"
    return 0
  fi
  if sudo -n test -r "$SECRETS/protect-admin-user" 2>/dev/null; then
    # One at a time, and stripped: these files do not reliably end with a newline,
    # and catting them together silently joins the username to the password.
    printf '%s\n%s\n' \
      "$(sudo -n cat "$SECRETS/protect-admin-user" | tr -d '\r\n')" \
      "$(sudo -n cat "$SECRETS/protect-admin-password" | tr -d '\r\n')"
    return 0
  fi
  return 1
}

custody() {
  # $1 is release|restore|status
  local creds user password
  if ! creds="$(credentials)"; then
    echo "  no controller credentials — set OSPREY_USER and OSPREY_PASSWORD" >&2
    return 1
  fi
  user="$(sed -n 1p <<<"$creds")"
  password="$(sed -n 2p <<<"$creds")"
  local args=(--host "$(detect_host)" --user "$user" --password "$password")
  [[ -n "$CAMERA_MAC" ]] && args+=(--mac "$CAMERA_MAC")
  ./custody.sh "$1" "${args[@]}"
}

resident_state() {
  have_container && sudo docker exec "$CONTAINER" systemctl is-active "$RESIDENT_UNIT" 2>/dev/null || echo absent
}

port_owner() {
  sudo ss -tlnp 2>/dev/null | awk -v p=":$1 " 'index($4, p) || index($4"  ", p) {print $NF; exit}'
}

detect_host() {
  [[ -n "$HOST" ]] && { echo "$HOST"; return; }
  ip -4 -o addr show scope global | awk '{split($4,a,"/"); print a[1]; exit}'
}

check_free() {
  local port="$1" what="$2" owner
  owner="$(port_owner "$port")"
  if [[ -n "$owner" ]]; then
    echo "  :$port ($what) is held by $owner — set the matching OSPREY_*_PORT and retry" >&2
    return 1
  fi
}

stop_resident() {
  [[ "$(resident_state)" == active ]] || return 0
  echo "  stopping resident controller ($RESIDENT_UNIT) to free :7442"
  sudo docker exec "$CONTAINER" systemctl stop "$RESIDENT_UNIT"
  for _ in {1..20}; do
    [[ -z "$(port_owner 7442)" ]] && return 0
    sleep 0.5
  done
  echo "  WARNING: :7442 still held by $(port_owner 7442)" >&2
}

start_resident() {
  have_container || return 0
  echo "  restoring resident controller ($RESIDENT_UNIT)"
  sudo docker exec "$CONTAINER" systemctl start "$RESIDENT_UNIT" || true
}

stop_osprey() {
  [[ -f "$PIDFILE" ]] || return 0
  local pid; pid="$(cat "$PIDFILE")"
  if kill -0 "$pid" 2>/dev/null; then
    echo "  stopping osprey (pid $pid)"
    kill "$pid" 2>/dev/null || true
    for _ in {1..20}; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
  fi
  rm -f "$PIDFILE"
}

case "${1:-status}" in
  take)
    host="$(detect_host)"
    echo "osprey taking over :7442 as $host"
    # Fill in the camera details from its secret file unless already set.
    if [[ -z "$CAMERA_HOST" || -z "$CAMERA_USER" || -z "$CAMERA_PASSWORD" ]] \
       && sudo -n test -r "$CAMERA_SECRET" 2>/dev/null; then
      CAMERA_HOST="${CAMERA_HOST:-$(sudo -n python3 -c "import json,sys;print(json.load(open('$CAMERA_SECRET'))['host'])")}"
      CAMERA_USER="${CAMERA_USER:-$(sudo -n python3 -c "import json,sys;print(json.load(open('$CAMERA_SECRET'))['user'])")}"
      CAMERA_PASSWORD="${CAMERA_PASSWORD:-$(sudo -n python3 -c "import json,sys;print(json.load(open('$CAMERA_SECRET'))['password'])")}"
    fi
    # Fail here rather than half way through, and never sit waiting for a
    # password on a terminal that may not exist.
    if ! sudo -n true 2>/dev/null; then
      echo "  this needs passwordless sudo for docker and ss — run 'sudo -v' first" >&2
      exit 1
    fi
    check_free "$INGEST_PORT" "media ingest"
    check_free "$SNAPSHOT_PORT" "snapshot upload"
    check_free "$RTSP_PORT" "rtsp"
    check_free "$ONVIF_PORT" "onvif"
    # Ask first, while the resident controller is still up to be asked.
    if ! custody release; then
      echo "  continuing anyway — the camera will connect but refuse orders" >&2
    fi
    stop_resident
    trap 'echo; stop_osprey; start_resident' EXIT INT TERM
    # Only pass --tracks when OSPREY_TRACKS is set; otherwise defer to osprey.json
    # / the built-in default (all H.264). CLI still wins over the file when set.
    ./run.sh --host "$host" ${TRACKS:+--tracks "$TRACKS"} \
      --ingest-port "$INGEST_PORT" --snapshot-port "$SNAPSHOT_PORT" \
      --rtsp-port "$RTSP_PORT" --onvif-port "$ONVIF_PORT" \
      --dump "$DUMP" --verbose >"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    sleep 2
    kill -0 "$(cat "$PIDFILE")" 2>/dev/null || { echo "osprey failed to start:"; tail -20 "$LOG"; exit 1; }
    echo "  osprey running, logging to $LOG, messages to $DUMP"
    echo "  rtsp://$host:$RTSP_PORT/${TRACKS%%,*}   onvif http://$host:$ONVIF_PORT/onvif/device_service"
    # Point the camera at us. Stopping the resident controller is not enough on
    # its own — a camera whose socket is merely taken reconnects as already-adopted
    # and refuses orders. The manage POST re-points it, so it does a fresh adoption
    # and says hello, which is what osprey answers.
    if [[ -n "$CAMERA_HOST" && -n "$CAMERA_USER" && -n "$CAMERA_PASSWORD" ]]; then
      # Releasing reboots the camera's management interface; wait for :443 back.
      echo "  waiting for the camera's management interface"
      for _ in $(seq 1 30); do
        code="$(curl -sk -m 4 -o /dev/null -w '%{http_code}' "https://$CAMERA_HOST/" 2>/dev/null || true)"
        [[ "$code" =~ ^(200|401|403)$ ]] && break
        sleep 3
      done
      echo "  pointing the camera at us via /api/1.2/manage"
      if ./manage.sh --camera "$CAMERA_HOST" --user "$CAMERA_USER" \
           --password "$CAMERA_PASSWORD" --host "$host" \
           --control-port 7442 --token "$CAMERA_TOKEN"; then
        echo "  done — the camera should dial us and say hello within a few seconds"
      else
        echo "  manage failed; see above. The camera will not come to us on its own." >&2
      fi
    else
      echo "  no camera credential set — osprey is listening, but nothing will point"
      echo "  the camera here. Set OSPREY_CAMERA_HOST/USER/PASSWORD, or the camera"
      echo "  stays with its current controller. (A merely-stolen socket is refused.)"
    fi
    echo "  Ctrl-C to stop and restore the resident controller"
    tail -f "$LOG"
    ;;

  give-back)
    stop_osprey
    start_resident
    echo "  waiting for the camera to find the resident controller again"
    sleep 10
    custody restore || echo "  adopt it from the console UI if this did not take" >&2
    echo "  done"
    ;;

  status)
    echo "  resident controller ($RESIDENT_UNIT): $(resident_state)"
    for entry in "7442:control" "$INGEST_PORT:ingest" "$SNAPSHOT_PORT:snapshots" \
                 "$RTSP_PORT:rtsp" "$ONVIF_PORT:onvif"; do
      port="${entry%%:*}"; what="${entry##*:}"
      owner="$(port_owner "$port")"
      printf '  :%-6s %-10s %s\n' "$port" "$what" "${owner:-free}"
    done
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "  osprey: running (pid $(cat "$PIDFILE"))"
    else
      echo "  osprey: not running"
    fi
    ;;

  *)
    echo "usage: $0 {take|give-back|status}" >&2
    exit 2
    ;;
esac
