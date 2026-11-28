ARG OPEN_TERMINAL_IMAGE=ghcr.io/open-webui/open-terminal:0.17.20-slim
FROM ${OPEN_TERMINAL_IMAGE}

ARG TERMINAL_APT_SOURCE_FORMAT
ARG TERMINAL_APT_SOURCE_HASH
ARG TERMINAL_APT_REPOSITORY_SOURCE_HASH
# loaf build --with-apt-repos: the [terminal_server] APT settings serve this step as in chat containers, each in a
# file named after its setting. apt_source_file replaces the base image's sources; apt_repository_dir adds its
# repository at the path of chat containers. The image keeps the base image's APT sources either way.
RUN --mount=type=secret,id=terminal-apt-source \
    --mount=type=secret,id=terminal-apt-repository-source \
    --mount=type=bind,from=apt-repository,target=/run/loaf-apt/repository \
    set -eu; \
    sources=; \
    if [ -n "${TERMINAL_APT_SOURCE_FORMAT:-}${TERMINAL_APT_REPOSITORY_SOURCE_HASH:-}" ]; then \
        mkdir /tmp/loaf-apt-sources; \
        if [ -n "${TERMINAL_APT_SOURCE_FORMAT:-}" ]; then \
            case "$TERMINAL_APT_SOURCE_FORMAT" in list|sources) ;; *) exit 1 ;; esac; \
            [ -f /run/secrets/terminal-apt-source ]; \
            printf '%s  %s\n' "$TERMINAL_APT_SOURCE_HASH" /run/secrets/terminal-apt-source | sha256sum --check --status; \
            cp /run/secrets/terminal-apt-source "/tmp/loaf-apt-sources/apt_source_file.$TERMINAL_APT_SOURCE_FORMAT"; \
            sources="-o Dir::Etc::SourceList=/dev/null"; \
        else \
            cp -a /etc/apt/sources.list.d/. /tmp/loaf-apt-sources/; \
        fi; \
        if [ -n "${TERMINAL_APT_REPOSITORY_SOURCE_HASH:-}" ]; then \
            printf '%s  %s\n' "$TERMINAL_APT_REPOSITORY_SOURCE_HASH" /run/secrets/terminal-apt-repository-source \
                | sha256sum --check --status; \
            cp /run/secrets/terminal-apt-repository-source /tmp/loaf-apt-sources/apt_repository_dir.list; \
        fi; \
        sources="$sources -o Dir::Etc::SourceParts=/tmp/loaf-apt-sources"; \
    fi; \
    apt-get $sources -o APT::Update::Error-Mode=any update; \
    apt-get $sources install -y --no-install-recommends \
        bash build-essential cmake ninja-build python3-aiohttp python3-pytest python3-sphinx sudo; \
    rm -rf /var/lib/apt/lists/* /tmp/loaf-apt-sources

COPY provisioner.py runner.py /opt/loaf-terminal/
# Chat containers run commands as root with apt available; sudo keeps the non-interactive frontend.
# Model commands have no terminal: apt installs without a confirmation and writes plain dpkg output.
RUN printf 'Defaults env_keep += "DEBIAN_FRONTEND"\n' > /etc/sudoers.d/loaf-terminal \
    && chmod 0440 /etc/sudoers.d/loaf-terminal \
    && printf 'APT::Get::Assume-Yes "true";\nDpkg::Use-Pty "0";\n' > /etc/apt/apt.conf.d/90loaf-terminal
ENV OPEN_TERMINAL_MULTI_USER=false \
    OPEN_TERMINAL_ENABLE_BROWSER=false \
    OPEN_TERMINAL_ENABLE_DESKTOP=false \
    OPEN_TERMINAL_ENABLE_NOTEBOOKS=false \
    OPEN_TERMINAL_LOG_DIR=/tmp/open-terminal
RUN mkdir -p /home/user /home/chat \
    && HOME=/tmp OPEN_TERMINAL_API_KEY=metadata-only python -c 'import asyncio,json; from open_terminal.main import app,get_config; json.dump({"config":asyncio.run(get_config()),"schema":app.openapi()},open("/opt/loaf-terminal/metadata.json","w"))'
