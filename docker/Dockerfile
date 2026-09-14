FROM python:3.13-slim-bookworm

ARG LAB_UID=1000
ARG LAB_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MYPY_CACHE_DIR=/tmp/mypy-cache

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
        mypy==1.18.2 \
  onvif-zeep==0.2.12 \
  paho-mqtt==2.1.0 \
  pytest==8.4.2 \
        uiprotect==16.10.0

RUN groupadd --gid "${LAB_GID}" lab \
    && useradd --uid "${LAB_UID}" --gid "${LAB_GID}" --create-home --shell /bin/bash lab \
    && mkdir -p /workspace /state /opt/lab /tmp/mypy-cache \
    && chown -R lab:lab /workspace /state /opt/lab /tmp/mypy-cache

WORKDIR /workspace
USER lab:lab
