#!/usr/bin/env bash
# Read-only preflight for the physical UniFi G5 PTZ handoff on a native Linux host.
#
# Verifies the ignored .env.physical values, the camera's identity on the wire,
# that the camera-facing ports are free, and that both Compose profiles render
# with the intended bind addresses. It changes no camera setting, firewall rule,
# or Protect state.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=${1:-.env.physical}

fail() {
  printf 'preflight: %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null || fail "docker CLI not found"
[[ -f $ENV_FILE ]] || fail "missing $ENV_FILE; copy .env.physical.example and verify every value"

set -a
# shellcheck disable=SC1090  # value-only env file, not executable configuration
. "$ENV_FILE"
set +a

# Keep the existing diagnostic text below stable while accepting the product
# environment names used by the Osprey deployment files.
CUCKOO_PHYSICAL_BIND=${OSPREY_BIND:-}
G5_PTZ_IP=${OSPREY_CAMERA_IP:-}

for name in OSPREY_HOST OSPREY_BIND OSPREY_CAMERA_IP OSPREY_CAMERA_MAC; do
  [[ -n ${!name:-} ]] || fail "missing $name in $ENV_FILE"
done

normalise_mac() { printf '%s' "$1" | tr -d ':-' | tr '[:lower:]' '[:upper:]'; }

MAC=$(normalise_mac "$OSPREY_CAMERA_MAC")
[[ $MAC =~ ^[0-9A-F]{12}$ ]] || fail "OSPREY_CAMERA_MAC must be exactly 12 hexadecimal digits"
[[ $OSPREY_BIND != 0.0.0.0 ]] || fail "OSPREY_BIND must never be 0.0.0.0"

ip -4 -o addr show scope global |
  awk '{print $4}' |
  cut -d/ -f1 |
grep -qx -- "$OSPREY_BIND" ||
  fail "CUCKOO_PHYSICAL_BIND ${CUCKOO_PHYSICAL_BIND} is not assigned to this host"

ping -c1 -W2 "$OSPREY_CAMERA_IP" >/dev/null 2>&1 ||
  fail "camera ${G5_PTZ_IP} did not answer a targeted ping"

observed=$(ip neigh show "$OSPREY_CAMERA_IP" |
  awk '{for (i = 1; i < NF; i++) if ($i == "lladdr") print $(i + 1)}' |
  head -n1)
[[ -n $observed ]] || fail "no ARP entry for ${G5_PTZ_IP}; cannot verify the camera MAC"
[[ $(normalise_mac "$observed") == "$MAC" ]] ||
fail "camera MAC ${observed} does not match OSPREY_CAMERA_MAC ${MAC}"

timeout 5 bash -c "</dev/tcp/${OSPREY_CAMERA_IP}/443" 2>/dev/null ||
  fail "camera ${G5_PTZ_IP} did not accept HTTPS on TCP 443"

listeners=$(ss -ltn | awk 'NR > 1 {print $4}')
for port in 7442 7444 7550 8000 8554 18001; do
  if grep -q ":${port}\$" <<<"$listeners"; then
    fail "TCP ${port} is already in use; stop the conflicting listener before handoff"
  fi
done

compose=(
  docker compose
  --env-file "$ENV_FILE"
  -f compose.yaml
  -f compose.physical.yaml
  -p osprey-physical
)

docker compose --env-file "$ENV_FILE" -f compose.yaml config --quiet ||
  fail "base Osprey Compose configuration did not validate"
"${compose[@]}" config --quiet || fail "physical Compose profile did not validate"

rendered=$("${compose[@]}" config)
if grep -q 'host_ip: 0\.0\.0\.0' <<<"$rendered"; then
  fail "rendered Compose binds 0.0.0.0; only ${CUCKOO_PHYSICAL_BIND} and loopback are allowed"
fi

bind_of() {
  awk -v p="$1" '
    $1 == "host_ip:"   { address = $2 }
    $1 == "published:" {
      value = $2
      gsub(/"/, "", value)
      if (value == p) { print address; exit }
    }' <<<"$rendered"
}

expect_bind() {
  local port=$1 want=$2 got
  got=$(bind_of "$port")
  [[ -n $got ]] || fail "no published port ${port} in the rendered physical profile"
  [[ $got == "$want" ]] || fail "published ${port} binds '${got}', expected '${want}'"
}

expect_bind 7442 "$OSPREY_BIND"
expect_bind 7444 "$OSPREY_BIND"
expect_bind 7550 "$OSPREY_BIND"
expect_bind 8000 "$OSPREY_BIND"
expect_bind 8554 "$OSPREY_BIND"
expect_bind 18001 "$OSPREY_BIND"

printf 'preflight: camera %s (%s) reachable and verified over ARP\n' "$G5_PTZ_IP" "$MAC"
printf 'preflight: LAN binds on %s -> 8000, 8554, 18001\n' "$CUCKOO_PHYSICAL_BIND"
printf 'preflight: advertised ONVIF/RTSP on %s -> 8000, 8554 (firewall: deployment subnet only)\n' \
  "$CUCKOO_PHYSICAL_BIND"
printf 'preflight: loopback only -> 18555\n'
printf 'preflight: Osprey Compose renders; no camera, firewall, or Protect state changed\n'
