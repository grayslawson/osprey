#!/bin/sh
# Point the Homebrew formula at a release.
#
#   scripts/sync-homebrew-tap.sh <upstream-checkout> <tap-checkout> <version>
#
# The formula installs the launcher scripts from the release's source archive,
# so the checksum belongs to that archive and is computed from it rather than
# copied from anywhere.  The script is idempotent: running it twice for the
# same version leaves the same formula.
set -eu

upstream="${1:?usage: sync-homebrew-tap.sh <upstream-checkout> <tap-checkout> <version>}"
target="${2:?usage: sync-homebrew-tap.sh <upstream-checkout> <tap-checkout> <version>}"
version="${3:?usage: sync-homebrew-tap.sh <upstream-checkout> <tap-checkout> <version>}"

case "$version" in
  *[!0-9A-Za-z.+-]*) echo "Invalid version: $version" >&2; exit 1 ;;
esac

archive_url="https://github.com/grayslawson/osprey/archive/refs/tags/v${version}.tar.gz"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL -o "$tmp/archive.tar.gz" "$archive_url"

if command -v sha256sum >/dev/null 2>&1; then
  checksum="$(sha256sum "$tmp/archive.tar.gz" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
  checksum="$(shasum -a 256 "$tmp/archive.tar.gz" | cut -d' ' -f1)"
else
  echo "no sha256sum or shasum to checksum the archive with" >&2
  exit 1
fi

mkdir -p "$target/Formula"
awk -v version="$version" -v url="$archive_url" -v checksum="$checksum" '
  /^  version "/ { print "  version \"" version "\""; next }
  /^  url "/ { print "  url \"" url "\""; next }
  /^  sha256 "/ { print "  sha256 \"" checksum "\""; next }
  { print }
' "$upstream/packaging/homebrew/osprey.rb" > "$target/Formula/osprey.rb"

echo "Pointed the tap formula at Osprey $version ($checksum)."
