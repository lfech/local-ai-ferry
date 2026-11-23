# Dockerfile - Ollama downloader plus atomic Modelfile export helper.
# Build this while online; models live in the named volume, not in image layers.

# --- Base image and persistent model-store contract ---
ARG OLLAMA_IMAGE=ollama/ollama:latest
FROM ${OLLAMA_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends rsync \
    && rm -rf /var/lib/apt/lists/*

# ollama pull writes both blobs and manifests here automatically.
ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/ollama/models \
    OLLAMA_NOPRUNE=true

# --- Helper installation ---
# Normalize CRLF even for source copied outside Git on a Windows host.
COPY export-modelfiles.sh /usr/local/bin/export-modelfiles
RUN sed -i 's/\r$//' /usr/local/bin/export-modelfiles \
    && chmod 0755 /usr/local/bin/export-modelfiles \
    && mkdir -p /ollama/models /ollama/config

# --- Server startup ---
# EXPOSE documents the port; Compose controls actual host port publication.
EXPOSE 11434
# Inherit the official image's Ollama entrypoint.
CMD ["serve"]
