#!/usr/bin/env bash
# Start the physical G5 PTZ controller on this VM (native Linux, rootless Podman).
#
# Builds the lab image if needed, starts only Cuckoo, waits for its health check,
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
  -p cuckoo-physical-lab
)

open_firewall() {
  # NixOS ships a default-DROP INPUT policy, so the published lab ports need an
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

allow_onvif_from_lab() {
  # Cuckoo advertises its LAN bind address in ONVIF XAddrs, so the container that
  # External consumers on the project bridge may need ONVIF/RTSP advertised
  # ports. Scope accepts to this project's own bridge subnet so the LAN cannot.
  local subnet port
  subnet=$(docker network inspect cuckoo-physical-lab_lab \
    --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null | head -n1)
  if [[ -z $subnet ]]; then
    printf 'firewall: lab subnet unknown; ONVIF/RTSP stay loopback-only\n'
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
      printf 'firewall: allowed %s -> tcp/%s (lab containers only)\n' "$subnet" "$port"
    fi
  done
}

open_firewall

"${compose[@]}" build osprey
"${compose[@]}" up -d --no-deps --wait --wait-timeout 120 osprey

"${compose[@]}" ps

printf '\nphysical listeners:\n'
ss -ltn | awk 'NR == 1 || $4 ~ /:(8000|8554|18001)$/'
