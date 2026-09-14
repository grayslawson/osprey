#!/usr/bin/env bash
# Start the physical G5 PTZ controller on this VM (native Linux, rootless Podman).
#
# Builds the image if needed, starts only Osprey, waits for its health check,
# and prints the listener table so the LAN binds can be verified before the
# camera is handed over.
set -euo pipefail

here=$(dirname "$0")
cd "${here}/.."
ENV_FILE=${1:-.env.physical}

"${here}/physical_preflight.sh" "$ENV_FILE"

compose=(
  docker compose
  --env-file "$ENV_FILE"
  -f compose.yaml
  -f compose.physical.yaml
  -p osprey-physical
)

open_firewall() {
  # Hosts with a default-DROP INPUT policy need the published camera ports to have an
  # explicit accept before the camera or a browser can reach them. Only these
  # four ports are opened, and physical_stop.sh closes them again.
  if ! sudo iptables -S nixos-fw >/dev/null 2>&1; then
    printf 'firewall: no nixos-fw chain found; leaving host firewall untouched\n'
    return 0
  fi
  for port in 7442 7444 7550 8000 8554 18001; do
    if sudo iptables -C nixos-fw -p tcp --dport "$port" -j nixos-fw-accept 2>/dev/null; then
      printf 'firewall: tcp/%s already allowed\n' "$port"
    else
      sudo iptables -I nixos-fw 3 -p tcp --dport "$port" -j nixos-fw-accept
      printf 'firewall: allowed tcp/%s\n' "$port"
    fi
  done
}

allow_onvif_from_deployment() {
  # Cuckoo advertises its LAN bind address in ONVIF XAddrs, so the container that
  # External consumers on the project bridge may need ONVIF/RTSP advertised
  # ports. Scope accepts to this project's own bridge subnet so the LAN cannot.
  local subnet port
  subnet=$(docker network inspect osprey-physical_default \
    --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null | head -n1)
  if [[ -z $subnet ]]; then
    printf 'firewall: deployment subnet unknown; ONVIF/RTSP stay loopback-only\n'
    return 0
  fi
  if ! sudo iptables -S nixos-fw >/dev/null 2>&1; then
    return 0
  fi
  for port in 8000 8554; do
    if sudo iptables -C nixos-fw -s "$subnet" -p tcp --dport "$port" -j nixos-fw-accept 2>/dev/null; then
      printf 'firewall: %s -> tcp/%s already allowed\n' "$subnet" "$port"
    else
      sudo iptables -I nixos-fw 3 -s "$subnet" -p tcp --dport "$port" -j nixos-fw-accept
      printf 'firewall: allowed %s -> tcp/%s (deployment containers only)\n' "$subnet" "$port"
    fi
  done
}

open_firewall
allow_onvif_from_deployment

"${compose[@]}" build osprey
"${compose[@]}" up -d --no-deps --wait --wait-timeout 120 osprey

"${compose[@]}" ps

printf '\nphysical listeners:\n'
ss -ltn | awk 'NR == 1 || $4 ~ /:(8000|8554|18001)$/'
