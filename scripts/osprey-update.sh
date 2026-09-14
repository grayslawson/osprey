#!/bin/sh
set -eu
file=./compose.yaml; image=''; apply=false
while [ "$#" -gt 0 ]; do case "$1" in --file|--image) [ "$#" -gt 1 ] || exit 2; eval "${1#--}='$2'"; shift 2;; --apply) apply=true; shift;; --dry-run) apply=false; shift;; --help) echo 'Usage: osprey-update.sh --image ghcr.io/grayslawson/osprey:vX.Y.Z [--file PATH] [--apply]'; exit 0;; *) exit 2;; esac; done
[ -n "$image" ] || { echo '--image is required (pinned tag or digest)' >&2; exit 2; }
case "$image" in *:latest|*@sha256:) echo 'refusing unpinned image' >&2; exit 2;; *[!A-Za-z0-9._:/@-]*) exit 2;; esac
[ -f "$file" ] || exit 1
runtime=docker; command -v docker >/dev/null 2>&1 || runtime=podman; command -v "$runtime" >/dev/null 2>&1 || exit 1
echo "deployment=$file image=$image runtime=$runtime"
[ "$apply" = true ] || { echo 'check only: use --apply to pull and recreate Osprey'; exit 0; }
"$runtime" compose -f "$file" config >/dev/null
"$runtime" compose -f "$file" pull osprey
"$runtime" compose -f "$file" up -d osprey
echo 'Update complete; rollback with the previous pinned --image.'
