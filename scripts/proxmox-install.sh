#!/bin/sh
# Proxmox guest helper; dry-run by default.
set -eu
usage() { echo 'Usage: proxmox-install.sh --host ADDRESS [--image IMAGE] [--runtime docker|podman] [--state-dir PATH] [--apply] [--force]'; }
host='' ; image=ghcr.io/grayslawson/osprey:latest ; runtime=docker ; state=/var/lib/osprey ; onvif=8000 ; rtsp=8554 ; apply=false ; force=false
while [ "$#" -gt 0 ]; do case "$1" in --host|--image|--runtime|--state-dir|--onvif-port|--rtsp-port) [ "$#" -gt 1 ] || exit 2; eval "${1#--}='$2'"; shift 2;; --apply) apply=true; shift;; --force) force=true; shift;; --dry-run) apply=false; shift;; --help) usage; exit 0;; *) usage >&2; exit 2;; esac; done
[ -n "$host" ] || { echo '--host is required' >&2; exit 2; }
case "$host" in *[!A-Za-z0-9._:%\[\]-]*) echo 'unsafe host' >&2; exit 2;; esac
case "$image" in ''|*[!A-Za-z0-9._:/@-]*) echo 'unsafe image' >&2; exit 2;; esac
case "$state" in /*) ;; *) echo 'state must be absolute' >&2; exit 2;; esac
[ "$state" != / ] || { echo 'refusing state root' >&2; exit 2; }
case "$state" in *[!A-Za-z0-9._/-]*) echo 'unsafe state path' >&2; exit 2;; esac
case "$onvif:$rtsp" in *[!0-9:]*|*:|:*) echo 'ports must be numeric' >&2; exit 2;; esac
[ "$onvif" -ge 1 ] 2>/dev/null && [ "$onvif" -le 65535 ] || exit 2; [ "$rtsp" -ge 1 ] 2>/dev/null && [ "$rtsp" -le 65535 ] || exit 2
arch=$(uname -m); case "$arch" in x86_64|amd64|aarch64|arm64) ;; *) exit 1;; esac
compose="$state/compose.yaml"; [ ! -e "$state" ] || [ -d "$state" ] || exit 1; [ ! -e "$compose" ] || [ "$force" = true ] || exit 1
echo "validated $arch guest; image=$image runtime=$runtime host=$host"
[ "$apply" = true ] || { echo "dry-run: would create $state and start Osprey"; exit 0; }; [ "$(id -u)" -eq 0 ] || exit 1
command -v "$runtime" >/dev/null || { command -v apt-get >/dev/null || exit 1; apt-get update; apt-get install -y "$runtime"; }
if "$runtime" compose version >/dev/null 2>&1; then compose_cmd="$runtime compose"; elif command -v "${runtime}-compose" >/dev/null 2>&1; then compose_cmd="${runtime}-compose"; else exit 1; fi
mkdir -p "$state"
cat > "$compose" <<EOF
services:
  osprey:
    image: "$image"
    command: ["python3", "main.py", "--host", "$host", "--cert", "/state/osprey.pem"]
    working_dir: /workspace/cuckoo
    environment: {PYTHONPATH: /workspace/pyunifiwire/src}
    volumes: ["$state:/state"]
    ports: ["$onvif:8000", "$rtsp:8554"]
    restart: unless-stopped
    read_only: true
    tmpfs: [/tmp:mode=1777]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
EOF
$compose_cmd -f "$compose" up -d
echo "Osprey started; inspect with: $compose_cmd -f $compose ps"
