#!/bin/sh
# Build the Chocolatey package for a release.
#
#   scripts/sync-chocolatey-feed.sh <upstream-checkout> <feed-checkout> <version>
#
# Chocolatey packages use NuGet versions, which are SemVer2: a pre-release is
# written "0.9.0-beta1", not "0.9.0b1" as PEP 440 spells it.  The version is
# converted here, and the package is written to the feed checkout.
#
# The script needs the NuGet client to pack.  It is installed in CI by
# NuGet/setup-nuget.
set -eu

upstream="${1:?usage: sync-chocolatey-feed.sh <upstream-checkout> <feed-checkout> <version>}"
target="${2:?usage: sync-chocolatey-feed.sh <upstream-checkout> <feed-checkout> <version>}"
version="${3:?usage: sync-chocolatey-feed.sh <upstream-checkout> <feed-checkout> <version>}"

case "$version" in
  *[!0-9A-Za-z.+-]*) echo "Invalid version: $version" >&2; exit 1 ;;
esac

# PEP 440 pre-release suffixes to NuGet pre-release labels.
case "$version" in
  *a[0-9]*) package_version="$(echo "$version" | sed 's/\(.*\)a\([0-9]*\)$/\1-alpha\2/')" ;;
  *b[0-9]*) package_version="$(echo "$version" | sed 's/\(.*\)b\([0-9]*\)$/\1-beta\2/')" ;;
  *rc[0-9]*) package_version="$(echo "$version" | sed 's/\(.*\)rc\([0-9]*\)$/\1-rc\2/')" ;;
  *) package_version="$version" ;;
esac

command -v nuget >/dev/null 2>&1 && has_nuget=1 || has_nuget=0
command -v docker >/dev/null 2>&1 && has_docker=1 || has_docker=0
if [ "$has_nuget" -eq 0 ] && [ "$has_docker" -eq 0 ]; then
  echo "packing needs the nuget client or docker" >&2
  exit 1
fi

# The nuspec names its files with a path relative to itself, so it is packed
# from a tree that keeps that layout: the nuspec in packaging/chocolatey/ and
# the compose file two levels up.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/packaging/chocolatey"
cp "$upstream/compose.release.yaml" "$tmp/compose.release.yaml"
cp -R "$upstream/packaging/chocolatey/tools" "$tmp/packaging/chocolatey/tools"
sed "s|<version>.*</version>|<version>$package_version</version>|" \
  "$upstream/packaging/chocolatey/osprey.nuspec" > "$tmp/packaging/chocolatey/osprey.nuspec"

if [ "$has_nuget" -eq 1 ]; then
  nuget pack "$tmp/packaging/chocolatey/osprey.nuspec" -OutputDirectory "$target"
else
  # The NuGet client is a .NET Framework application and needs mono, which the
  # Linux runners do not carry.  The official Chocolatey image has a native
  # client instead, and it packs the same nuspec.
  docker run --rm \
    --volume "$tmp:/work" \
    --volume "$target:/out" \
    "chocolatey/choco:${CHOCO_IMAGE:-v2.7.5-linux}" \
    choco pack /work/packaging/chocolatey/osprey.nuspec --out /out
fi

echo "Packed osprey $package_version into $target."
