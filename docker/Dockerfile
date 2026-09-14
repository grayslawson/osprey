FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e

ARG OSPREY_UID=1000
ARG OSPREY_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MYPY_CACHE_DIR=/tmp/mypy-cache \
    PYTHONPATH=/workspace/pyunifiwire/src:/workspace/cuckoo

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Keep the runtime image small: ONVIF is implemented in-process and the
# acceptance-test, type-checking, and Protect custody dependencies belong in a
# development environment. Paho is optional at runtime but installed so MQTT
# can be enabled without rebuilding the image.
RUN printf '%s\n' \
      'paho-mqtt==2.1.0 --hash=sha256:6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee' \
      > /tmp/requirements-runtime.txt \
 && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-runtime.txt \
 && rm /tmp/requirements-runtime.txt

RUN groupadd --gid "${OSPREY_GID}" osprey \
    && useradd --uid "${OSPREY_UID}" --gid "${OSPREY_GID}" --create-home --shell /bin/bash osprey \
    && mkdir -p /workspace /state /opt/osprey /tmp/mypy-cache \
    && chown -R osprey:osprey /workspace /state /opt/osprey /tmp/mypy-cache

# The release image is built from the repository root. Keep the runtime image
# self-contained; compose.release.yaml does not mount the checkout.
COPY --chown=osprey:osprey cuckoo /workspace/cuckoo
COPY --chown=osprey:osprey pyunifiwire /workspace/pyunifiwire
COPY --chown=osprey:osprey scripts/osprey scripts/osprey-doctor.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/osprey /usr/local/bin/osprey-doctor.sh

WORKDIR /workspace/cuckoo
USER osprey:osprey
