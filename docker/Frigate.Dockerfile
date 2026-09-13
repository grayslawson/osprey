FROM ghcr.io/blakeblackshear/frigate:0.18.0-rc1@sha256:7060d1ec944e4276ae815a0025644de5ac52df978d061dc046876423c4ec5102

COPY docker/patch_frigate_ptz_recovery.py /tmp/patch_frigate_ptz_recovery.py
RUN python3 /tmp/patch_frigate_ptz_recovery.py /opt/frigate \
    && python3 -m py_compile \
        /opt/frigate/frigate/ptz/onvif.py \
        /opt/frigate/frigate/ptz/autotrack.py \
    && rm /tmp/patch_frigate_ptz_recovery.py
