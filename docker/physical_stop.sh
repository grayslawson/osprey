#!/usr/bin/env bash
# Stop a physical G5 PTZ deployment and release its LAN listeners.
#
# Stopping Osprey releases only this host's listeners; it does not reset,
# unmanage, or re-adopt the camera.
set -euo pipefail

here=$(dirname "$0")
cd "${here}/.."
ENV_FILE=${1:-.env.physical}

compose=(
  docker compose
  --env-file "$ENV_FILE"
  -f compose.yaml
  -f compose.physical.yaml
  -p osprey-physical
)

# The bridge subnet has to be read while the project still exists.
deployment_subnet=$(docker network inspect osprey-physical_default \
  --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null | head -n1 || true)

"${compose[@]}" down --remove-orphans

close_firewall() {
  # Removes exactly the accepts physical_start.sh adds: the camera ports plus the
  # ONVIF/RTSP accepts scoped to the deployment subnet. None are in this host's
  # baseline allowlist, so this cannot drop a pre-existing rule.
  if ! sudo iptables -S nixos-fw >/dev/null 2>&1; then
    return 0
  fi
  local port
  for port in 7442 7444 7550 8000 8554 18001; do
    while sudo iptables -C nixos-fw -p tcp --dport "$port" -j nixos-fw-accept 2>/dev/null; do
      sudo iptables -D nixos-fw -p tcp --dport "$port" -j nixos-fw-accept
      printf 'firewall: closed tcp/%s\n' "$port"
    done
  done
  [[ -n $deployment_subnet ]] || return 0
  for port in 8000 8554; do
    while sudo iptables -C nixos-fw -s "$deployment_subnet" -p tcp --dport "$port" -j nixos-fw-accept 2>/dev/null; do
      sudo iptables -D nixos-fw -s "$deployment_subnet" -p tcp --dport "$port" -j nixos-fw-accept
      printf 'firewall: closed %s -> tcp/%s\n' "$deployment_subnet" "$port"
    done
  done
}

close_firewall

if ss -ltn | awk 'NR > 1 {print $4}' | grep -Eq ':(8000|8554|18001)$'; then
  printf 'stop: physical listeners are still bound; check for a stray relay\n' >&2
  ss -ltn | awk 'NR == 1 || $4 ~ /:(8000|8554|18001)$/'
  exit 1
fi

printf 'stop: no physical listeners remain on 8000, 8554, 18001\n'
printf 'stop: no camera reset, unmanage, or re-adoption was performed\n'
