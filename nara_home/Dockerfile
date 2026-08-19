FROM ghcr.io/home-assistant/base-debian:trixie

ARG BUILD_VERSION="0.4.12"
ARG BUILD_ARCH="amd64"

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

COPY constraints.txt /tmp/constraints.txt
COPY nara_home_mcp-0.4.12-py3-none-any.whl /tmp/nara_home_mcp-0.4.12-py3-none-any.whl

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates python3 python3-venv \
    && python3 -m venv /opt/nara \
    && /opt/nara/bin/python -m pip install \
        --no-cache-dir \
        --only-binary=:all: \
        --constraint /tmp/constraints.txt \
        /tmp/nara_home_mcp-0.4.12-py3-none-any.whl \
    && groupadd --system --gid 999 nara \
    && useradd --system --uid 999 --gid 999 --no-create-home --home-dir /nonexistent nara \
    && find /opt/nara -type d -exec chmod 0555 {} + \
    && find /opt/nara -type f -exec chmod 0444 {} + \
    && find /opt/nara/bin -type f -exec chmod 0555 {} + \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /tmp/constraints.txt /tmp/nara_home_mcp-0.4.12-py3-none-any.whl

COPY --chmod=0555 run.sh /usr/local/bin/nara-home-launcher
COPY --chmod=0444 entities.yaml /usr/local/share/nara-home-entities.yaml

ENV HOME=/tmp \
    PATH=/opt/nara/bin:$PATH \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    ENTITIES_FILE=/usr/local/share/nara-home-entities.yaml \
    HA_CONFIG_ROOT=/homeassistant_config \
    HA_CONFIG_REQUIRE_READ_ONLY_MOUNT=true \
    ALLOW_DYNAMIC_ENTITIES=false

# HAOS protected Apps do not grant runtime SETUID/SETGID capabilities. Start
# directly as the fixed unprivileged account and fail closed in the launcher.
USER 999:999

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/opt/nara/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

ENTRYPOINT []
CMD ["/usr/local/bin/nara-home-launcher"]
