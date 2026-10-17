# syntax=docker/dockerfile:1
# Osprey release image.
#
# Build from the repository root:
#   docker build -f docker/Dockerfile --build-arg OSPREY_VERSION="$(cat VERSION)" -t osprey:local .
#
# The image runs the controller by default (see docker/entrypoint.sh). It is
# designed for `--read-only --cap-drop ALL --user 1000:1000`; the only writable
# paths it needs are the state volume (/var/lib/osprey) and /tmp.
#
# Debian slim rather than distroless: the runtime shells out to openssl (first
# run certificate) and ffmpeg (operator preview). The base is pinned by digest
# of the multi-arch index; Dependabot (docker ecosystem) proposes bumps.
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

FROM ${PYTHON_IMAGE}

ARG OSPREY_VERSION=0.0.0+unknown
ARG OSPREY_REVISION=unknown
ARG OSPREY_CREATED=1970-01-01T00:00:00Z
ARG OSPREY_UID=1000
ARG OSPREY_GID=1000

LABEL org.opencontainers.image.title="Osprey" \
      org.opencontainers.image.description="ONVIF/RTSP controller for supported Ubiquiti PTZ cameras, for use with an existing Frigate installation" \
      org.opencontainers.image.url="https://github.com/grayslawson/osprey" \
      org.opencontainers.image.source="https://github.com/grayslawson/osprey" \
      org.opencontainers.image.documentation="https://github.com/grayslawson/osprey/blob/main/docs/installation.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="Osprey contributors" \
      org.opencontainers.image.version="${OSPREY_VERSION}" \
      org.opencontainers.image.revision="${OSPREY_REVISION}" \
      org.opencontainers.image.created="${OSPREY_CREATED}"

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/osprey/pyunifiwire/src \
    OSPREY_STATE_DIR=/var/lib/osprey

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        ffmpeg \
        openssl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# paho-mqtt is optional at runtime but installed so MQTT can be enabled
# without rebuilding the image. Hash-pinned.
RUN printf '%s\n' \
      'paho-mqtt==2.1.0 --hash=sha256:6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee' \
      > /tmp/requirements-runtime.txt \
    && python -m pip install --require-hashes -r /tmp/requirements-runtime.txt \
    && rm /tmp/requirements-runtime.txt

RUN groupadd --gid "${OSPREY_GID}" osprey \
    && useradd --uid "${OSPREY_UID}" --gid "${OSPREY_GID}" --no-create-home \
        --home-dir /var/lib/osprey --shell /usr/sbin/nologin osprey \
    && install -d -o "${OSPREY_UID}" -g "${OSPREY_GID}" -m 0750 /var/lib/osprey

# Application code is owned by root and read-only to the runtime user.
COPY VERSION /opt/osprey/VERSION
COPY cuckoo /opt/osprey/cuckoo
COPY pyunifiwire/src /opt/osprey/pyunifiwire/src
COPY pyunifiwire/LICENSE /opt/osprey/pyunifiwire/LICENSE
COPY LICENSE NOTICE /opt/osprey/
COPY docker/entrypoint.sh /usr/local/bin/osprey-entrypoint
COPY docker/cuckoo_health.py /usr/local/bin/osprey-healthcheck
COPY scripts/osprey scripts/osprey-doctor.sh /opt/osprey/scripts/
RUN chmod 0755 /usr/local/bin/osprey-entrypoint /usr/local/bin/osprey-healthcheck \
        /opt/osprey/scripts/osprey /opt/osprey/scripts/osprey-doctor.sh \
    && rm -rf /opt/osprey/cuckoo/tests /opt/osprey/cuckoo/harness \
    && python -m compileall -q /opt/osprey/cuckoo /opt/osprey/pyunifiwire/src

# Camera control (TLS), snapshot upload, media ingest, ONVIF/operator console,
# RTSP, and WS-Discovery. Prefer host networking; see compose.release.yaml.
EXPOSE 7442/tcp 7444/tcp 7550/tcp 8000/tcp 8554/tcp 3702/udp

VOLUME ["/var/lib/osprey"]
WORKDIR /var/lib/osprey
USER ${OSPREY_UID}:${OSPREY_GID}

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "/usr/local/bin/osprey-healthcheck"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/osprey-entrypoint"]
CMD ["run"]
