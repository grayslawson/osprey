#!/bin/sh
# Safely update a release deployment without touching any other Compose service.
# The default mode is a read-only check. Pulling/restarting requires --apply.
set -eu

usage() {
    cat <<'EOF'
Usage:
  osprey-update.sh [options]

Safe default (check only):
  osprey-update.sh --file compose.release.yaml --env-file .env.release

Options:
  --file PATH       Compose file (default: ./compose.release.yaml)
  --env-file PATH   Compose interpolation environment file
  --image IMAGE     Exact pinned image tag or digest to review/apply
  --latest          Resolve the latest GitHub release (check only unless
                    combined with --apply)
  --runtime NAME    auto, docker, or podman (default: auto)
  --apply           Pull and recreate only the osprey service
  --dry-run         Explicitly select check-only mode (the default)
  --help            Show this help

Examples:
  osprey-update.sh --env-file /var/lib/osprey/.env \
    --image ghcr.io/grayslawson/osprey:v1.2.3
  osprey-update.sh --env-file /var/lib/osprey/.env --latest --apply
  osprey-update.sh --image ghcr.io/grayslawson/osprey:v1.2.2 --apply  # rollback
EOF
}

die() {
    printf 'osprey-update: %s\n' "$*" >&2
    exit 2
}

file=./compose.release.yaml
env_file=
image=
latest=false
apply=false
runtime=auto

while [ "$#" -gt 0 ]; do
    case "$1" in
        --file|--env-file|--image|--runtime)
            [ "$#" -gt 1 ] || die "$1 requires a value"
            case "$1" in
                --file) file=$2 ;;
                --env-file) env_file=$2 ;;
                --image) image=$2 ;;
                --runtime) runtime=$2 ;;
            esac
            shift 2
            ;;
        --latest) latest=true; shift ;;
        --apply) apply=true; shift ;;
        --dry-run|--check) apply=false; shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $1 (use --help)" ;;
    esac
done

[ -f "$file" ] || die "Compose file does not exist: $file"
[ -z "$env_file" ] || [ -f "$env_file" ] || die "environment file does not exist: $env_file"

case "$runtime" in
    auto|docker|podman) ;;
    *) die "runtime must be auto, docker, or podman" ;;
esac

# Compose implementations are deliberately selected, not guessed from the
# presence of a binary. A Docker installation may not have the Compose plugin;
# Podman can provide `podman compose` through an external provider.
compose_mode=
if [ "$runtime" = auto ] || [ "$runtime" = docker ]; then
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        compose_mode=docker
    elif [ "$runtime" = docker ]; then
        die 'docker compose is not available'
    fi
fi
if [ -z "$compose_mode" ] && { [ "$runtime" = auto ] || [ "$runtime" = podman ]; }; then
    if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
        compose_mode=podman
    elif [ "$runtime" = podman ]; then
        die 'podman compose is not available'
    fi
fi
[ -n "$compose_mode" ] || die 'neither docker compose nor podman compose is available'

compose() {
    case "$compose_mode" in
        docker) docker compose "$@" ;;
        podman) podman compose "$@" ;;
    esac
}

compose_file() {
    if [ -n "$env_file" ]; then
        compose --env-file "$env_file" -f "$file" "$@"
    else
        compose -f "$file" "$@"
    fi
}

# Read only the simple OSPREY_IMAGE=... form from an env file. Compose remains
# the authority for interpolation; this never executes the file as shell code.
configured_image=
if [ -n "$env_file" ]; then
    configured_image=$(sed -n \
        -e 's/^OSPREY_IMAGE[[:space:]]*=[[:space:]]*//p' \
        -e 's/^export[[:space:]]\{1,\}OSPREY_IMAGE[[:space:]]*=[[:space:]]*//p' \
        "$env_file" | sed -n '1p' | sed 's/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//' || true)
fi
[ -n "$configured_image" ] || configured_image=${OSPREY_IMAGE:-}

validate_image() {
    candidate=$1
    [ -n "$candidate" ] || die 'image is empty'
    case "$candidate" in
        *[!A-Za-z0-9._:/@-]*) die "image contains unsafe characters: $candidate" ;;
        *:latest|*:main|*:master) die "refusing mutable image tag: $candidate" ;;
        *@sha256:) die "image digest is incomplete: $candidate" ;;
    esac
    case "$candidate" in
        *@sha256:*)
            digest=${candidate##*@sha256:}
            case "$digest" in
                [!0-9A-Fa-f]*|*[!0-9A-Fa-f]*) die "image digest must be hexadecimal: $candidate" ;;
            esac
            [ "${#digest}" -eq 64 ] || die "image digest must contain 64 hexadecimal characters: $candidate"
            ;;
        *:*)
            tag=${candidate##*:}
            case "$tag" in
                v[0-9]*.[0-9]*.[0-9]*) ;;
                *) die "image tag must be a version such as v1.2.3 or a digest: $candidate" ;;
            esac
            ;;
        *) die "image must include a version tag or sha256 digest: $candidate" ;;
    esac
}

latest_release_tag() {
    repo=${OSPREY_GITHUB_REPOSITORY:-grayslawson/osprey}
    api=${OSPREY_RELEASE_API_URL:-https://api.github.com/repos/$repo/releases/latest}
    response=
    if command -v curl >/dev/null 2>&1; then
        response=$(curl -fsSL --connect-timeout 5 --max-time 20 \
            -H 'Accept: application/vnd.github+json' "$api") || die "could not query $api"
    elif command -v python3 >/dev/null 2>&1; then
        response=$(python3 - "$api" <<'PY'
import sys
import urllib.request

request = urllib.request.Request(
    sys.argv[1], headers={"Accept": "application/vnd.github+json", "User-Agent": "osprey-update"}
)
with urllib.request.urlopen(request, timeout=20) as response:
    sys.stdout.write(response.read().decode("utf-8"))
PY
        ) || die "could not query $api"
    else
        die 'latest release lookup requires curl or python3'
    fi
    tag=$(printf '%s' "$response" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | sed -n '1p')
    case "$tag" in
        v[0-9]*.[0-9]*.[0-9]*) printf '%s\n' "$tag" ;;
        *) die 'GitHub did not return a stable release tag' ;;
    esac
}

if [ "$latest" = true ]; then
    [ -z "$image" ] || die '--latest and --image are mutually exclusive'
    base=${configured_image:-ghcr.io/grayslawson/osprey}
    case "$base" in
        *@*) base=${base%@*} ;;
        */*:*) base=${base%:*} ;;
    esac
    image=$base:$(latest_release_tag)
fi

[ -n "$image" ] || {
    if [ -n "$configured_image" ]; then
        image=$configured_image
    else
        die 'provide --image IMAGE or --latest (use a pinned release)'
    fi
}
validate_image "$image"

# Validate interpolation in both modes. This does not pull or restart.
OSPREY_IMAGE=$image
export OSPREY_IMAGE
compose_file config >/dev/null || die 'Compose configuration is invalid or missing required environment values'

printf 'deployment: %s\n' "$file"
[ -z "$env_file" ] || printf 'environment: %s\n' "$env_file"
printf 'target image: %s\n' "$image"
if [ "$apply" = false ]; then
    printf 'mode: check only (no image pull and no restart)\n'
    printf 'apply after review: %s' "$0"
    printf ' --file %s' "$file"
    [ -z "$env_file" ] || printf ' --env-file %s' "$env_file"
    printf ' --image %s --apply\n' "$image"
    exit 0
fi

printf 'mode: apply (pulling and recreating only service osprey)\n'
compose_file pull osprey
compose_file up -d --no-deps osprey
compose_file ps osprey
printf 'update complete; rollback with the previous pinned image and --apply\n'
