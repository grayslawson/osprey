#!/bin/sh
# Refresh osprey/third_party/raptor_vision from a raptor-vision release tag. Usage: scripts/sync-raptor-vision.sh v0.2.0
set -eu

tag=${1:?usage: sync-raptor-vision.sh vX.Y.Z}
repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
dest=$repo_dir/osprey/third_party/raptor_vision
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

git clone --quiet --depth 1 --branch "$tag" https://github.com/grayslawson/raptor-vision.git "$work/src"
rm -rf "$dest"
mkdir -p "$dest"
for module in __init__ types providers tasks labels label geometry interest frigate; do
	cp "$work/src/src/raptor_vision/$module.py" "$dest/$module.py"
done
cp "$work/src/LICENSE" "$dest/LICENSE"
printf '%s\n' "${tag#v}" >"$dest/VERSION"
echo "raptor_vision ${tag#v} vendored into osprey/third_party/raptor_vision"
