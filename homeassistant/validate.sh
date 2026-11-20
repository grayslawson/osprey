#!/bin/sh
set -eu

root="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
manifest="$root/addon/osprey/config.yaml"
repo="$root/addon/repository.json"
test -s "$manifest"
test -s "$repo"
test -x "$root/addon/osprey/run.sh"
test -s "$root/../packaging/homeassistant-repo/repository.yaml"
grep -q '^name: Osprey$' "$manifest"
grep -q '^slug: osprey$' "$manifest"
grep -q '^image: ghcr.io/grayslawson/{arch}-osprey-addon$' "$manifest"
grep -q '^url: https://github.com/grayslawson/osprey$' "$manifest"
grep -q '^host_network: true$' "$manifest"
grep -q '"url": "https://github.com/grayslawson/osprey"' "$repo"
python3 -m json.tool "$repo" >/dev/null
# The add-on manifest version must match the release version, or the release
# workflow refuses to build the add-on image.
version="$(tr -d '[:space:]' < "$root/../VERSION")"
manifest_version="$(awk -F: '/^version:/ {gsub(/[^0-9A-Za-z.+-]/, "", $2); print $2; exit}' "$manifest")"
if [ "$manifest_version" != "$version" ]; then
  echo "Home Assistant manifest version ($manifest_version) does not match VERSION ($version)" >&2
  exit 1
fi
echo 'Home Assistant add-on static checks passed.'
