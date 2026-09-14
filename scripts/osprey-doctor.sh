#!/bin/sh
# Read-only deployment diagnostics; never changes camera, firewall, Compose,
# or Frigate state.
set -u

usage() {
    cat <<'EOF'
Usage: osprey doctor [options]

Options:
  --host ADDRESS       Osprey advertised host (or OSPREY_HOST)
  --camera ADDRESS     camera address to route-test
  --frigate URL        existing Frigate URL to health-check
  --config PATH        JSON config to validate (default: cuckoo.json)
  --ports LIST         comma-separated ports (default: 7442,7444,7550,8000,8554)
  --help
EOF
}

host=${OSPREY_HOST:-}
camera=${OSPREY_CAMERA_IP:-}
frigate=${OSPREY_FRIGATE_URL:-}
config_file=cuckoo.json
ports=7442,7444,7550,8000,8554
errors=0
warnings=0
ok() { printf 'OK      %s\n' "$*"; }
warn() { printf 'WARNING %s\n' "$*"; warnings=$((warnings + 1)); }
fail() { printf 'ERROR   %s\n' "$*"; errors=$((errors + 1)); }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --host|--camera|--frigate|--config|--ports)
            [ "$#" -gt 1 ] || { usage >&2; exit 2; }
            case "$1" in
                --host) host=$2 ;; --camera) camera=$2 ;; --frigate) frigate=$2
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

if [ -f "$config_file" ]; then
    if command -v python3 >/dev/null 2>&1 && PYTHONPATH="${PYTHONPATH:-}cuckoo" python3 - "$config_file" <<'PY'
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

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok 'Docker Compose is available'
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    ok 'Podman Compose is available'
else
    fail 'neither Docker Compose nor Podman Compose is available'
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
    if command -v ip >/dev/null 2>&1 && ip route get "$camera" >/dev/null 2>&1; then
        ok "camera has a kernel route: $camera"
    else
        fail "camera has no kernel route: $camera"
    fi
fi

if [ -n "$frigate" ]; then
    case "$frigate" in http://*|https://*) ;; *) fail 'Frigate URL must start with http:// or https://' ;; esac
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS --connect-timeout 3 --max-time 5 "$frigate/api/version" >/dev/null 2>&1; then
            ok "Frigate API responded: $frigate"
        else
            warn "Frigate API did not respond (it may require auth or be firewalled): $frigate"
        fi
    else
        warn 'curl is unavailable; skipped Frigate reachability check'
    fi
fi

printf 'Summary: %s error(s), %s warning(s)\n' "$errors" "$warnings"
[ "$errors" -eq 0 ]
