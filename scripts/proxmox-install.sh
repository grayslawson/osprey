#!/bin/sh
# Proxmox guest helper. Defaults to a non-mutating dry-run; requires --apply.
# Usage: proxmox-install.sh --host ADDRESS [--image IMAGE] [--runtime docker|podman]
#   [--state-dir PATH] [--onvif-port PORT] [--rtsp-port PORT] [--apply] [--force]
set -eu
usage() { sed -n '1,6p' "$0"; }
host= image=ghcr.io/grayslawson/osprey:latest runtime=docker state=/var/lib/osprey onvif=8000 rtsp=8554 apply=false force=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --host) [ "$#" -gt 1 ] || exit 2; host=$2; shift 2;;
    --image) image=$2; shift 2;; --runtime) runtime=$2; shift 2;; --state-dir) state=$2; shift 2;;
    --onvif-port) onvif=$2; shift 2;; --rtsp-port) rtsp=$2; shift 2;;
    --apply) apply=true; shift;; --force) force=true; shift;; --dry-run) apply=false; shift;;
    --help) usage; exit 0;; *) echo "Unknown option: $1" >&2; usage >&2; exit 2;;
  esac
done
[ -n "$host" ] || { echo '--host is required' >&2; exit 2; }
case "$runtime" in docker|podman) ;; *) echo '--runtime must be docker or podman' >&2; exit 2;; esac
case "$host" in *[!A-Za-z0-9._:%\[\]-]*|*[	 \r\n]*) echo 'host contains unsafe characters' >&2; exit 2;; esac
case "$image" in ''|*[!A-Za-z0-9._:/@-]*) echo 'image contains unsafe characters' >&2; exit 2;; esac
case "$state" in ''|/|*[!A-Za-z0-9._/-]*) echo 'state directory must be a safe absolute path' >&2; exit 2;; esac
case "$state" in /*) ;; *) echo 'state directory must be absolute' >&2; exit 2;; esac
case "$onvif:$rtsp" in *[!0-9:]*|*:|:*) echo 'ports must be numeric' >&2; exit 2;; esac
[ "$onvif" -ge 1 ] 2>/dev/null && [ "$onvif" -le 65535 ] || { echo 'invalid ONVIF port' >&2; exit 2; }
[ "$rtsp" -ge 1 ] 2>/dev/null && [ "$rtsp" -le 65535 ] || { echo 'invalid RTSP port' >&2; exit 2; }
arch=$(uname -m)
case "$arch" in x86_64|amd64|aarch64|arm64) ;; *) echo "Unsupported architecture: $arch" >&2; exit 1;; esac
[ "$(id -u)" -eq 0 ] || [ "$apply" != true ] || { echo '--apply must run as root' >&2; exit 1; }
compose="$state/compose.yaml"
[ ! -e "$state" ] || [ -d "$state" ] || { echo "state path is not a directory: $state" >&2; exit 1; }
[ ! -e "$compose" ] || [ "$force" = true ] || { echo "$compose exists; use --force" >&2; exit 1; }
echo "validated $arch guest; image=$image runtime=$runtime host=$host"
if [ "$apply" != true ]; then echo "dry-run: would create $state, write compose.yaml, pull image, and start Osprey"; exit 0; fi
if ! command -v "$runtime" >/dev/null; then command -v apt-get >/dev/null || { echo "Install $runtime first" >&2; exit 1; }; apt-get update; apt-get install -y "$runtime"; fi
if "$runtime" compose version >/dev/null 2>&1; then
  compose_cmd="$runtime compose"
elif command -v "${runtime}-compose" >/dev/null 2>&1; then
  compose_cmd="${runtime}-compose"
else
  echo "$runtime Compose support is required (install its compose plugin)" >&2
  exit 1
fi
mkdir -p "$state"
cat > "$compose" <<EOF
services:
  osprey:
    image: $image
    command: ["python3", "main.py", "--host", "$host", "--cert", "/state/osprey.pem"]
    working_dir: /workspace/cuckoo
    environment: {PYTHONPATH: /workspace/pyunifiwire/src}
    volumes: [$state:/state]
    ports: ["$onvif:8000", "$rtsp:8554"]
    restart: unless-stopped
    read_only: true
    tmpfs: [/tmp:mode=1777]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
EOF
$compose_cmd -f "$compose" up -d
echo "Osprey started; inspect with: $compose_cmd -f $compose ps"
