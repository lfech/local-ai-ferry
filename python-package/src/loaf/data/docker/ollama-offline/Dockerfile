# Dockerfile - GPU Ollama with startup import and offline defaults.
# Weights, projectors and exported Modelfiles share /ollama across both images.

# --- Base image and offline runtime defaults ---
ARG OLLAMA_IMAGE=ollama/ollama:latest
FROM ${OLLAMA_IMAGE}

# Do not prune blobs before the startup script recreates their manifests.
ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/ollama/models \
    OLLAMA_NOPRUNE=true \
    OLLAMA_CONTEXT_LENGTH=16384 \
    OLLAMA_NO_CLOUD=1

# --- Import wrapper installation ---
# NVIDIA's drivers and Container Toolkit belong on the Docker host.
# The official Ollama image already includes its GPU inference libraries.
# Strip Windows CRLF before the kernel reads the script's Linux shebang.
COPY start-ollama-offline.sh /usr/local/bin/start-ollama-offline
RUN sed -i 's/\r$//' /usr/local/bin/start-ollama-offline \
    && chmod 0755 /usr/local/bin/start-ollama-offline \
    && mkdir -p /ollama/models /ollama/config

# --- PID 1 wrapper owns server startup, imports and shutdown ---
EXPOSE 11434
ENTRYPOINT ["/usr/local/bin/start-ollama-offline"]
# Clear the inherited CMD so the base image's arguments are not appended here.
CMD []
