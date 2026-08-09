ARG TARGETARCH
FROM python:3.11-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3 AS base

ARG LAB_VERSION
ARG TESTED_EHA_VERSION
ARG EHA_SOURCE_REPOSITORY
ARG EHA_SOURCE_REVISION
ARG TARGETARCH

# MCP servers use the same OS-level tools as the resident image, but this image
# does not install or start an LLM harness or the resident daemon.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates jq git mosquitto-clients \
    libasound2-plugins pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

# Match the resident EHA image's Python runtime dependencies so every MCP server
# can at least initialize and answer tools/list in the release canary.
RUN python3 -m pip install --break-system-packages --no-cache-dir faster-whisper --quiet 2>/dev/null || true
RUN python3 -m pip install --break-system-packages --no-cache-dir "pysilero-vad==3.2.0" \
    && python3 -c "from pysilero_vad import SileroVoiceActivityDetector; SileroVoiceActivityDetector()"

FROM base AS arch-amd64
ENV HA_BUILD_ARCH=amd64

FROM base AS arch-arm64
ENV HA_BUILD_ARCH=aarch64

FROM arch-${TARGETARCH} AS final

ARG LAB_VERSION
ARG TESTED_EHA_VERSION
ARG EHA_SOURCE_REPOSITORY
ARG EHA_SOURCE_REVISION
ARG TARGETARCH

LABEL io.hass.version="${LAB_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${HA_BUILD_ARCH}"

# EHA sourceは読み取り対象として同梱するだけで、EHAのentrypointやdaemonは起動しない。
COPY .upstream/embodied-ha-repo/embodied_ha/ /app/
COPY embodied_ha_mcp_lab/ /lab/embodied_ha_mcp_lab/

RUN python3 /lab/embodied_ha_mcp_lab/source_identity.py write \
        --root /app \
        --output /lab/.eha-source-identity.json \
        --lab-version "${LAB_VERSION}" \
        --tested-eha-version "${TESTED_EHA_VERSION}" \
        --build-arch "${TARGETARCH}" \
        --source-repository "${EHA_SOURCE_REPOSITORY}" \
        --source-revision "${EHA_SOURCE_REVISION}"

ENV EHA_MCP_LAB_SOURCE_DIR=/app \
    EHA_MCP_LAB_BUILD_IDENTITY=/lab/.eha-source-identity.json \
    EHA_MCP_LAB_AUTH_FILE=/config/embodied-ha-mcp-lab/eha-mcp-lab-token.config.toml \
    PYTHONPATH=/lab

CMD ["python3", "-m", "embodied_ha_mcp_lab.mcp_lab"]
