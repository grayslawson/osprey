# Release builds use the repository root as the Docker build context:
#   docker build -f homeassistant/addon/osprey/Dockerfile .
# Home Assistant's builder publishes the resulting per-architecture image.
ARG BUILD_FROM=python:3.13-slim-bookworm
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/osprey/pyunifiwire/src:/opt/osprey/cuckoo

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir onvif-zeep==0.2.12 uiprotect==16.10.0

COPY cuckoo /opt/osprey/cuckoo
COPY pyunifiwire /opt/osprey/pyunifiwire
COPY homeassistant/addon/osprey/run.sh /run.sh

RUN chmod 0755 /run.sh \
    && mkdir -p /data/osprey \
    && addgroup --system osprey \
    && adduser --system --ingroup osprey osprey \
    && chown -R osprey:osprey /opt/osprey /data/osprey

WORKDIR /opt/osprey/cuckoo
USER osprey
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/run.sh"]
