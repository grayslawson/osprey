#!/bin/sh
# Read-only deployment diagnostics; never changes camera, firewall, Compose,
# or Frigate state.
set -u

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# Empty when only the launcher is installed (Homebrew, Chocolatey); the checks
# that need the controller's code then say so instead of failing.
controller_dir=$(CDPATH='' cd -- "$script_dir/../osprey" 2>/dev/null && pwd) || controller_dir=

usage() {
    cat <<'EOF'
Usage: osprey doctor [options]

Options:
  --host ADDRESS       Osprey advertised host (or OSPREY_HOST)
  --camera ADDRESS     camera address to route-test
  --callback URL        callback URL that must be reachable (or OSPREY_CALLBACK_URL)
  --frigate URL        existing Frigate URL to health-check
  --config PATH        JSON config to validate (default: osprey.json)
  --ports LIST         comma-separated ports (default: 7442,7444,7550,8000,8554)
  --help
EOF
}

host=${OSPREY_HOST:-}
camera=${OSPREY_CAMERA_IP:-}
callback=${OSPREY_CALLBACK_URL:-}
frigate=${OSPREY_FRIGATE_URL:-}
config_file=osprey.json
[ -f osprey.json ] || [ ! -f cuckoo.json ] || config_file=cuckoo.json  # pre-0.9 name
ports=7442,7444,7550,8000,8554
errors=0
warnings=0
ok() { printf 'OK      %s\n' "$*"; }
warn() { printf 'WARNING %s\n' "$*"; warnings=$((warnings + 1)); }
fail() { printf 'ERROR   %s\n' "$*"; errors=$((errors + 1)); }

# The release image has python3 but no ip or curl; fall back to the standard
# library so doctor gives the same answers inside the container.
has_route() {
    if command -v ip >/dev/null 2>&1; then
        ip route get "$1" >/dev/null 2>&1
    else
        python3 -c 'import socket,sys; s=socket.socket(socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET, socket.SOCK_DGRAM); s.connect((sys.argv[1], 9))' "$1" 2>/dev/null
    fi
}
http_ok() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --connect-timeout 3 --max-time 5 "$1" >/dev/null 2>&1
    else
        python3 -c 'import sys,urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5).read(1)' "$1" >/dev/null 2>&1
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --host|--camera|--callback|--frigate|--config|--ports)
            [ "$#" -gt 1 ] || { usage >&2; exit 2; }
            case "$1" in
                --host) host=$2 ;; --camera) camera=$2 ;; --callback) callback=$2 ;; --frigate) frigate=$2
                ;; --config) config_file=$2 ;; --ports) ports=$2 ;;
            esac
            shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [ -z "$host" ]; then
    fail 'set --host or OSPREY_HOST so callbacks and advertised URIs are routable'
elif [ "$host" = 0.0.0.0 ] || [ "$host" = :: ]; then
    fail '--host must be a concrete LAN address, not a wildcard'
elif command -v python3 >/dev/null 2>&1 && python3 - "$host" <<'PY'
import ipaddress, re, sys
value = sys.argv[1]
try:
    ipaddress.ip_address(value.strip('[]'))
except ValueError:
    if re.fullmatch(r'[A-Za-z0-9_.-]{1,253}', value) is None:
        raise SystemExit(1)
PY
then
    ok "advertised host is valid: $host"
else
    fail "invalid advertised host: $host"
fi

if [ -f "$config_file" ] && [ -z "$controller_dir" ]; then
    warn "configuration not validated: the Osprey controller is not installed next to this launcher"
elif [ -f "$config_file" ]; then
    if command -v python3 >/dev/null 2>&1 && PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}$controller_dir" python3 - "$config_file" <<'PY'
import sys
import config
try:
    value = config.merged(config.load(sys.argv[1]))
    config.validate_runtime(value)
except (OSError, ValueError, TypeError) as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1)
PY
    then ok "configuration is valid: $config_file"; else fail "configuration is invalid: $config_file"; fi
else
    warn "configuration file not found: $config_file (CLI/environment settings may be intentional)"
fi

# Protect dual adoption: the physical camera must not be adoptable by Protect.
if [ -f "$config_file" ] && grep -q '"onvif_read_only"[[:space:]]*:[[:space:]]*[0-9]' "$config_file" 2>/dev/null; then
    if [ -n "$controller_dir" ] && [ -n "${OSPREY_UNIFI_API_KEY_FILE:-}" ] && [ -n "${OSPREY_UNIFI_CONSOLE:-}" ] && [ -n "$camera" ]; then
        if PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}$controller_dir" python3 "$controller_dir/protect_guard.py" \
            --console "$OSPREY_UNIFI_CONSOLE" --camera-ip "$camera" >/dev/null 2>&1; then
            ok 'UniFi firewall guard keeps Protect off the physical camera'
        else
            warn 'no UniFi firewall guard for the physical camera: run osprey protect-guard (docs/protect-guard.md)'
        fi
    else
        warn 'read-only ONVIF persona is on: make sure Protect cannot adopt the physical camera (osprey protect-guard, docs/protect-guard.md)'
    fi
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok 'Docker Compose is available'
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    ok 'Podman Compose is available'
else
    warn 'neither Docker Compose nor Podman Compose is available (needed only for a compose deployment)'
fi

if command -v ss >/dev/null 2>&1; then
    old_ifs=$IFS; IFS=,
    for port in $ports; do
        case "$port" in ''|*[!0-9]*) fail "invalid port in --ports: $port"; continue ;; esac
        if ss -H -ltn 2>/dev/null | awk -v p=":$port" '$4 ~ p {found=1} END {exit !found}'; then
            warn "TCP port $port is already listening"
        else
            ok "TCP port $port is available"
        fi
    done
    IFS=$old_ifs
else
    warn 'ss is unavailable; skipped local port checks'
fi

if [ -n "$camera" ]; then
    if has_route "$camera"; then
        ok "camera has a kernel route: $camera"
    else
        fail "camera has no kernel route: $camera"
    fi
fi

if [ -n "$callback" ]; then
    case "$callback" in
        http://*|https://*) ;;
        *) fail 'callback URL must start with http:// or https://' ;;
    esac
    if http_ok "$callback"; then
        ok "callback URL responded: $callback"
    else
        fail "callback URL did not respond: $callback"
    fi
fi

if [ -n "$frigate" ]; then
    case "$frigate" in http://*|https://*) ;; *) fail 'Frigate URL must start with http:// or https://' ;; esac
    if http_ok "$frigate/api/version"; then
        ok "Frigate API responded: $frigate"
    else
        warn "Frigate API did not respond (it may require auth or be firewalled): $frigate"
    fi
fi

printf 'Summary: %s error(s), %s warning(s)\n' "$errors" "$warnings"
[ "$errors" -eq 0 ]
