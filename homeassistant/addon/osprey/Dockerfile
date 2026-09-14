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
 && printf '%s\n' \
      'paho-mqtt==2.1.0 --hash=sha256:6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee' \
      > /tmp/requirements-runtime.txt \
 && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-runtime.txt \
 && rm /tmp/requirements-runtime.txt

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
