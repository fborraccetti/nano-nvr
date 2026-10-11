# Fixed base image on purpose: the Supervisor passes its own s6-overlay base as
# BUILD_FROM, whose /init refuses to start when it is not PID 1.
FROM alpine:3.22

# ffmpeg does the video work, python3 runs the scheduler, tzdata makes the
# TZ variable set by Home Assistant work (file names and days use local time).
# hadolint ignore=DL3018
RUN apk add --no-cache ffmpeg python3 tzdata

ARG BUILD_ARCH
ARG BUILD_VERSION=dev
ENV NANO_NVR_VERSION=${BUILD_VERSION}

COPY app/ /app/
LABEL \
    io.hass.name="Nano NVR" \
    io.hass.description="Lightweight RTSP recorder for Home Assistant" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.licenses="MIT"

CMD ["python3", "-u", "/app/nano_nvr.py"]
