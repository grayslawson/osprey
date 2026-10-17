#!/bin/sh
# Osprey guest helper for a Proxmox VM/LXC (or any Debian-like host) that runs
# the release image with Docker or Podman. Dry-run by default.
#
# This does not create a VM or LXC. For a native LXC created from the Proxmox
# host, see packaging/proxmox/ (pending the first GitHub release).
set -eu

usage() {
  cat <<'EOF'
Usage: proxmox-install.sh --host ADDRESS --image IMAGE [options]

  --host ADDRESS          this guest's LAN address, as reached by the camera (required)
  --image IMAGE           immutable release tag (vX.Y.Z) or @sha256 digest (required)
  --bind-address ADDRESS  local address for Osprey's listeners (default: --host)
  --runtime NAME          docker or podman (default: docker)
  --state-dir PATH        compose file and state location (default: /var/lib/osprey)
  --onvif-port PORT       ONVIF/operator console port (default: 8000)
  --rtsp-port PORT        RTSP port (default: 8554)
  --apply                 write the Compose file and start Osprey
  --force                 overwrite an existing compose.yaml
  --dry-run               only validate and print the plan (default)

Osprey runs with host networking: the camera dials ADDRESS on TCP 7442, 7444
and 7550; clients use the ONVIF and RTSP ports; WS-Discovery uses UDP 3702.
EOF
}

die() { printf 'proxmox-install: %s\n' "$*" >&2; exit 2; }

host='' ; bind='' ; image='' ; runtime=docker ; state=/var/lib/osprey
onvif=8000 ; rtsp=8554 ; apply=false ; force=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --host|--bind-address|--image|--runtime|--state-dir|--onvif-port|--rtsp-port)
      [ "$#" -gt 1 ] || { usage >&2; exit 2; }
      case "$1" in
        --host) host=$2 ;;
        --bind-address) bind=$2 ;;
        --image) image=$2 ;;
        --runtime) runtime=$2 ;;
        --state-dir) state=$2 ;;
        --onvif-port) onvif=$2 ;;
        --rtsp-port) rtsp=$2 ;;
      esac
      shift 2
      ;;
    --apply) apply=true; shift ;;
    --force) force=true; shift ;;
    --dry-run) apply=false; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[ -n "$host" ] || die '--host is required'
[ -n "$image" ] || die '--image is required; use an immutable release tag or digest'
[ -n "$bind" ] || bind=$host
case "$host" in *[!A-Za-z0-9._:%\[\]-]*) die 'unsafe host' ;; esac
case "$bind" in *[!A-Za-z0-9._:%\[\]-]*) die 'unsafe bind address' ;; esac
case "$host" in 0.0.0.0|::|'[::]'|127.*|localhost) die '--host must be a LAN address the camera can reach' ;; esac
case "$image" in *[!A-Za-z0-9._:/@-]*) die 'unsafe image' ;; esac
case "$image" in *:latest|*:main|*:master) die 'image must use an immutable release tag or digest' ;; esac
case "$image" in
  *@sha256:*)
    digest=${image##*@sha256:}
    [ "${#digest}" -eq 64 ] || die 'image digest must contain 64 hexadecimal characters'
    case "$digest" in *[!0-9A-Fa-f]*) die 'image digest must be hexadecimal' ;; esac
    ;;
  *:v[0-9]*.[0-9]*.[0-9]*) ;;
  *) die 'image must include a version tag (vX.Y.Z) or sha256 digest' ;;
esac
case "$runtime" in docker|podman) ;; *) die 'runtime must be docker or podman' ;; esac
case "$state" in /*) ;; *) die 'state must be absolute' ;; esac
[ "$state" != / ] || die 'refusing state root'
case "$state" in */../*|*/..) die 'state must not contain parent-directory segments' ;; esac
case "$state" in *[!A-Za-z0-9._/-]*) die 'unsafe state path' ;; esac
for port in "$onvif" "$rtsp"; do
  case "$port" in ''|*[!0-9]*) die "port must be numeric: $port" ;; esac
  if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then die "port out of range: $port"; fi
done
arch=$(uname -m)
case "$arch" in x86_64|amd64|aarch64|arm64) ;; *) die "unsupported architecture: $arch" ;; esac
compose="$state/compose.yaml"
[ ! -e "$state" ] || [ -d "$state" ] || die "state path exists and is not a directory: $state"
[ ! -e "$compose" ] || [ "$force" = true ] || die "$compose exists; pass --force to overwrite"

extra_args=''
[ "$onvif" = 8000 ] || extra_args="--onvif-port $onvif"
[ "$rtsp" = 8554 ] || extra_args="${extra_args:+$extra_args }--rtsp-port $rtsp"

printf 'validated %s guest; image=%s runtime=%s host=%s bind=%s\n' "$arch" "$image" "$runtime" "$host" "$bind"
printf 'ports: tcp 7442 7444 7550 %s %s, udp 3702 (host networking)\n' "$onvif" "$rtsp"
if [ "$apply" != true ]; then
  printf 'dry-run: would write %s and start Osprey\n' "$compose"
  exit 0
fi
[ "$(id -u)" -eq 0 ] || die '--apply must run as root'

if ! command -v "$runtime" >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || die "$runtime is not installed and apt-get is unavailable"
  apt-get update
  if [ "$runtime" = docker ]; then
    apt-get install -y docker.io docker-compose
  else
    apt-get install -y podman podman-compose
  fi
fi
if "$runtime" compose version >/dev/null 2>&1; then
  compose_cmd="$runtime compose"
elif command -v "${runtime}-compose" >/dev/null 2>&1; then
  compose_cmd="${runtime}-compose"
else
  die "no Compose implementation found for $runtime"
fi

# The container runs as uid/gid 1000; the state volume must be writable by it.
install -d -m 0750 "$state"
install -d -m 0750 -o 1000 -g 1000 "$state/data"
cat > "$compose" <<EOF
# Generated by proxmox-install.sh. Host networking: see compose.release.yaml.
name: osprey
services:
  osprey:
    image: "$image"
    network_mode: host
    environment:
      OSPREY_HOST: "$host"
      OSPREY_BIND: "$bind"
      OSPREY_EXTRA_ARGS: "$extra_args"
      OSPREY_HEALTH_PORT: "$onvif"
    volumes: ["$state/data:/var/lib/osprey"]
    user: "1000:1000"
    restart: unless-stopped
    read_only: true
    tmpfs: [/tmp:mode=1777]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    stop_grace_period: 10s
EOF
# shellcheck disable=SC2086  # compose_cmd is "runtime compose" or one binary
$compose_cmd -f "$compose" up -d
printf 'Osprey started; inspect with: %s -f %s ps\n' "$compose_cmd" "$compose"
printf 'Open http://%s:%s/ ; set OSPREY_SETUP=1 in %s for the first-run wizard.\n' "$host" "$onvif" "$compose"
