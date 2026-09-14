#!/bin/sh
set -eu

root="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
manifest="$root/addon/osprey/config.yaml"
repo="$root/addon/repository.json"
test -s "$manifest"
test -s "$repo"
test -x "$root/addon/osprey/run.sh"
grep -q '^name: Osprey$' "$manifest"
grep -q '^slug: osprey$' "$manifest"
grep -q '^image: ghcr.io/grayslawson/{arch}-osprey-addon$' "$manifest"
grep -q '^url: https://github.com/grayslawson/osprey$' "$manifest"
grep -q '^host_network: true$' "$manifest"
grep -q '"url": "https://github.com/grayslawson/osprey"' "$repo"
python3 -m json.tool "$repo" >/dev/null
echo 'Home Assistant add-on static checks passed.'
