ARG OPEN_TERMINAL_IMAGE=ghcr.io/open-webui/open-terminal:0.17.20-slim
FROM ${OPEN_TERMINAL_IMAGE}

ARG TERMINAL_APT_SOURCE_FORMAT
ARG TERMINAL_APT_SOURCE_HASH
RUN --mount=type=secret,id=terminal-apt-source set -eu; \
    if [ -n "${TERMINAL_APT_SOURCE_FORMAT:-}" ]; then \
        case "$TERMINAL_APT_SOURCE_FORMAT" in list|sources) ;; *) exit 1 ;; esac; \
        [ -s /run/secrets/terminal-apt-source ]; \
        printf '%s  %s\n' "$TERMINAL_APT_SOURCE_HASH" /run/secrets/terminal-apt-source | sha256sum --check --status; \
        rm -f /etc/apt/sources.list; \
        mkdir -p /etc/apt/sources.list.d; \
        rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; \
        cp /run/secrets/terminal-apt-source "/etc/apt/sources.list.d/loaf-terminal.$TERMINAL_APT_SOURCE_FORMAT"; \
        chmod 0644 "/etc/apt/sources.list.d/loaf-terminal.$TERMINAL_APT_SOURCE_FORMAT"; \
    fi; \
    apt-get -o APT::Update::Error-Mode=any update; \
    apt-get install -y --no-install-recommends \
        bash build-essential cmake ninja-build python3-aiohttp python3-pytest python3-sphinx sudo; \
    rm -rf /var/lib/apt/lists/*

COPY provisioner.py runner.py /opt/loaf-terminal/
# Chat containers run commands as root with apt available; sudo keeps the non-interactive frontend.
RUN printf 'Defaults env_keep += "DEBIAN_FRONTEND"\n' > /etc/sudoers.d/loaf-terminal \
    && chmod 0440 /etc/sudoers.d/loaf-terminal
ENV OPEN_TERMINAL_MULTI_USER=false \
    OPEN_TERMINAL_ENABLE_BROWSER=false \
    OPEN_TERMINAL_ENABLE_DESKTOP=false \
    OPEN_TERMINAL_ENABLE_NOTEBOOKS=false \
    OPEN_TERMINAL_LOG_DIR=/tmp/open-terminal
RUN mkdir -p /home/user /home/chat \
    && HOME=/tmp OPEN_TERMINAL_API_KEY=metadata-only python -c 'import asyncio,json; from open_terminal.main import app,get_config; json.dump({"config":asyncio.run(get_config()),"schema":app.openapi()},open("/opt/loaf-terminal/metadata.json","w"))'
