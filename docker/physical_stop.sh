#!/usr/bin/env bash
# Stop the physical G5 PTZ lab and release its LAN listeners.
#
# Rolling the camera itself back to Protect is a separate, credentialed action:
# re-adopt the G5 in the Protect console after this script reports the listeners
# are released.
set -euo pipefail

here=$(dirname "$0")
cd "${here}/.."
ENV_FILE=${1:-.env.physical}

compose=(
  docker compose
  --env-file "$ENV_FILE"
  -f compose.yaml
  -f compose.physical.yaml
  -p cuckoo-physical-lab
)

# The bridge subnet has to be read while the project still exists.
lab_subnet=$(docker network inspect cuckoo-physical-lab_lab \
  --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null | head -n1 || true)

"${compose[@]}" --profile frigate down --remove-orphans

close_firewall() {
  # Removes exactly the accepts physical_start.sh adds: the four lab ports plus the
  # ONVIF/RTSP accepts scoped to the lab subnet. None of them are in this host's
  # baseline allowlist, so this cannot drop a pre-existing rule.
  if ! sudo iptables -S nixos-fw >/dev/null 2>&1; then
    return 0
  fi
  local port
  for port in 7442 7444 7550 15001 18001; do
    while sudo iptables -C nixos-fw -p tcp --dport "$port" -j nixos-fw-accept 2>/dev/null; do
      sudo iptables -D nixos-fw -p tcp --dport "$port" -j nixos-fw-accept
      printf 'firewall: closed tcp/%s\n' "$port"
    done
  done
  [[ -n $lab_subnet ]] || return 0
  for port in 8000 8554; do
    while sudo iptables -C nixos-fw -s "$lab_subnet" -p tcp --dport "$port" -j nixos-fw-accept 2>/dev/null; do
      sudo iptables -D nixos-fw -s "$lab_subnet" -p tcp --dport "$port" -j nixos-fw-accept
      printf 'firewall: closed %s -> tcp/%s\n' "$lab_subnet" "$port"
    done
  done
}

close_firewall

if ss -ltn | awk 'NR > 1 {print $4}' | grep -Eq ':(7442|7444|7550|15001|18001)$'; then
  printf 'stop: physical listeners are still bound; check for a stray relay\n' >&2
  ss -ltn | awk 'NR == 1 || $4 ~ /:(7442|7444|7550|15001|18001)$/'
  exit 1
fi

printf 'stop: no physical listeners remain on 7442, 7444, 7550, 15001, 18001\n'
printf 'stop: re-adopt the G5 camera in Protect to restore its original ownership\n'
