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

rm -rf "$target/src"
mkdir -p "$addon" "$target/src"
cp -R "$upstream/osprey" "$target/src/osprey"
cp -R "$upstream/pyunifiwire" "$target/src/pyunifiwire"
cp "$upstream/homeassistant/addon/osprey/run.sh" "$addon/run.sh"
cp "$upstream/homeassistant/addon/osprey/README.md" "$addon/README.md"
chmod 0755 "$addon/run.sh"

# The manifest names the version and the image it installs.  Both follow the
# release, so they are written rather than copied.
awk -v version="$version" '
  /^version:/ { print "version: \"" version "\""; next }
  /^image:/ { print "image: ghcr.io/grayslawson/{arch}-osprey-addon:" version; next }
  { print }
' "$upstream/homeassistant/addon/osprey/config.yaml" > "$addon/config.yaml"

cp "$upstream/packaging/homeassistant-repo/repository.yaml" "$target/repository.yaml"

# The build context is the app repository root, so the application source is
# vendored under src/ and the copy paths differ from the ones in the Osprey
# repository.
cat > "$addon/Dockerfile" <<'DOCKERFILE'
# The add-on image is built from this repository.  The application source is
# vendored under src/, so the image can be built without a checkout of the
# Osprey repository.
ARG BUILD_FROM=python:3.13-slim-bookworm
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/osprey/pyunifiwire/src:/opt/osprey/osprey

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/* \
 && printf '%s\n' \
      'paho-mqtt==2.1.0 --hash=sha256:6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee' \
      > /tmp/requirements-runtime.txt \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-runtime.txt \
    && rm /tmp/requirements-runtime.txt

COPY osprey/src/osprey /opt/osprey/osprey
COPY osprey/src/pyunifiwire /opt/osprey/pyunifiwire
COPY osprey/run.sh /run.sh

RUN chmod 0755 /run.sh \
    && mkdir -p /data/osprey \
    && addgroup --system osprey \
    && adduser --system --ingroup osprey osprey \
    && chown -R osprey:osprey /opt/osprey /data/osprey

WORKDIR /opt/osprey/osprey
USER osprey
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/run.sh"]
DOCKERFILE

echo "Synced the Osprey $version add-on into $target."
