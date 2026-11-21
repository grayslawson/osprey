#!/bin/sh
# Mirror the Home Assistant add-on into the Home Assistant app repository.
#
#   scripts/sync-ha-apps.sh <upstream-checkout> <app-repository-checkout>
#
# Home Assistant reads an app repository from the root of a Git repository:
# a repository.yaml file at the root and one directory per add-on, named after
# the add-on slug.  The Osprey repository keeps the add-on in
# homeassistant/addon/osprey/, which is a source copy rather than an app
# repository, so the add-on has to be mirrored into a repository of its own
# for Home Assistant to list it.
#
# The mirror is a copy, not a link: the app repository vendors the application
# source under the add-on directory so it can be built on its own.
#
# The script is idempotent.  Running it twice leaves the same tree, so a
# workflow can run it and commit only when something changed.
set -eu

upstream="${1:?usage: sync-ha-apps.sh <upstream-checkout> <app-repository-checkout>}"
target="${2:?usage: sync-ha-apps.sh <upstream-checkout> <app-repository-checkout>}"

version="$(tr -d '[:space:]' < "$upstream/VERSION")"
addon="$target/osprey"

# The Supervisor builds an add-on with its own directory as the build context,
# so the vendored source lives inside it.
rm -rf "$target/src" "$addon/src"
mkdir -p "$addon/src"
cp -R "$upstream/osprey" "$addon/src/osprey"
cp -R "$upstream/pyunifiwire" "$addon/src/pyunifiwire"
rm -rf "$addon/src/osprey/tests" "$addon/src/osprey/harness" "$addon/src/pyunifiwire/tests"
find "$addon/src" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$upstream/homeassistant/addon/osprey/run.sh" "$addon/run.sh"
cp "$upstream/homeassistant/addon/osprey/README.md" "$addon/README.md"
cp "$upstream/homeassistant/addon/osprey/DOCS.md" "$addon/DOCS.md"
cp "$upstream/homeassistant/addon/osprey/CHANGELOG.md" "$addon/CHANGELOG.md"
rm -rf "$addon/translations"
cp -R "$upstream/homeassistant/addon/osprey/translations" "$addon/translations"
chmod 0755 "$addon/run.sh"

# The manifest names the version and the image it installs.  Both follow the
# release, so they are written rather than copied.
awk -v version="$version" '
  /^version:/ { print "version: \"" version "\""; next }
  /^image:/ { print "image: ghcr.io/grayslawson/{arch}-osprey-addon:" version; next }
  { print }
' "$upstream/homeassistant/addon/osprey/config.yaml" > "$addon/config.yaml"

cp "$upstream/packaging/homeassistant-repo/repository.yaml" "$target/repository.yaml"

# Same image as the upstream add-on, with the copy paths rewritten for the
# vendored layout.
sed -e 's|^COPY osprey /opt/osprey/osprey$|COPY src/osprey /opt/osprey/osprey|' \
    -e 's|^COPY pyunifiwire /opt/osprey/pyunifiwire$|COPY src/pyunifiwire /opt/osprey/pyunifiwire|' \
    -e 's|^COPY homeassistant/addon/osprey/run.sh /run.sh$|COPY run.sh /run.sh|' \
    "$upstream/homeassistant/addon/osprey/Dockerfile" > "$addon/Dockerfile"
if grep -q '^COPY \(osprey\|pyunifiwire\|homeassistant\)' "$addon/Dockerfile"; then
  echo "sync-ha-apps.sh: an upstream COPY line was not rewritten" >&2
  exit 1
fi

echo "Synced the Osprey $version add-on into $target."
