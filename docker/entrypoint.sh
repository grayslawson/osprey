#!/bin/sh
# Container entrypoint for the Osprey release image (run under tini).
#
#   run [FLAGS...]     start the controller (default)
#   doctor [OPTS...]   read-only deployment diagnostics
#   healthcheck        exit 0 when the ONVIF/console listener accepts TCP
#   version            print the packaged Osprey version
#   -<flag> ...        shorthand for `run -<flag> ...`
#   anything else      executed verbatim (for debugging)
#
# Environment (all optional unless noted):
#   OSPREY_HOST        REQUIRED unless the config file sets "host": the LAN
#                      address the camera dials back to. Never a bridge IP.
#   OSPREY_STATE_DIR   writable state directory (default /var/lib/osprey)
#   OSPREY_CONFIG_FILE JSON config (default $OSPREY_STATE_DIR/osprey.json; a
#                      missing file means defaults)
#   OSPREY_CERT_FILE   control-channel PEM (default $OSPREY_STATE_DIR/osprey.pem)
#   OSPREY_ANNOUNCE    "false" disables WS-Discovery multicast Hello (use with
#                      bridge networking)
#   OSPREY_SETUP       "1" opens the browser first-run wizard until the wizard
#                      has written a host into the config file
#   OSPREY_EXTRA_ARGS  extra controller flags, whitespace separated
set -eu

state_dir=${OSPREY_STATE_DIR:-/var/lib/osprey}
export OSPREY_STATE_DIR="$state_dir"
if [ -z "${OSPREY_CONFIG_FILE:-}" ]; then
    OSPREY_CONFIG_FILE=$state_dir/osprey.json
    # Upgrade path: images before 0.1.0 wrote the wizard config as cuckoo.json.
    if [ ! -e "$OSPREY_CONFIG_FILE" ] && [ -e "$state_dir/cuckoo.json" ]; then
        OSPREY_CONFIG_FILE=$state_dir/cuckoo.json
    fi
fi
export OSPREY_CONFIG_FILE

die() {
    printf 'osprey: %s\n' "$*" >&2
    exit 64
}

run_controller() {
    if [ ! -d "$state_dir" ] || [ ! -w "$state_dir" ]; then
        die "state directory $state_dir is not writable by uid $(id -u); mount a volume there (chown 1000:1000 for bind mounts)"
    fi
    case "${OSPREY_HOST:-}" in
        0.0.0.0|::|'[::]') die 'OSPREY_HOST must be a concrete LAN address, not a wildcard' ;;
    esac
    set -- --cert "${OSPREY_CERT_FILE:-$state_dir/osprey.pem}" "$@"
    if [ -n "${OSPREY_HOST:-}" ]; then
        set -- --host "$OSPREY_HOST" "$@"
    fi
    case "${OSPREY_ANNOUNCE:-true}" in
        false|0|no|off) set -- --no-announce "$@" ;;
    esac
    if [ -n "${OSPREY_EXTRA_ARGS:-}" ]; then
        # Word splitting is intentional: flags only, no shell evaluation.
        # shellcheck disable=SC2086
        set -- "$@" $OSPREY_EXTRA_ARGS
    fi
    exec python3 /opt/osprey/cuckoo/main.py "$@"
}

if [ "$#" -eq 0 ]; then
    set -- run
fi

case "$1" in
    run) shift; run_controller "$@" ;;
    -*) run_controller "$@" ;;
    doctor) shift; exec /opt/osprey/scripts/osprey-doctor.sh --config "$OSPREY_CONFIG_FILE" "$@" ;;
    healthcheck) shift; exec python3 /usr/local/bin/osprey-healthcheck "$@" ;;
    version) cat /opt/osprey/VERSION ;;
    *) exec "$@" ;;
esac
