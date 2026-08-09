FROM python:3.11-slim-bookworm

ARG LAB_VERSION
ARG TESTED_EHA_VERSION
ARG EHA_SOURCE_REPOSITORY
ARG EHA_SOURCE_REVISION
ARG TARGETARCH

# EHA sourceは読み取り対象として同梱するだけで、EHAのentrypointやdaemonは起動しない。
COPY .upstream/embodied-ha-repo/embodied_ha/ /app/
COPY embodied_ha_mcp_lab/mcp_lab.py /lab/mcp_lab.py
COPY embodied_ha_mcp_lab/source_identity.py /lab/source_identity.py

RUN python3 /lab/source_identity.py write \
        --root /app \
        --output /lab/.eha-source-identity.json \
        --lab-version "${LAB_VERSION}" \
        --tested-eha-version "${TESTED_EHA_VERSION}" \
        --build-arch "${TARGETARCH}" \
        --source-repository "${EHA_SOURCE_REPOSITORY}" \
        --source-revision "${EHA_SOURCE_REVISION}"

ENV EHA_MCP_LAB_SOURCE_DIR=/app \
    EHA_MCP_LAB_BUILD_IDENTITY=/lab/.eha-source-identity.json

CMD ["python3", "/lab/mcp_lab.py"]
