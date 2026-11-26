# Dockerfile - Open WebUI configured for the local Ollama service.
# Application data persists at /app/backend/data through the Compose volume.

# --- Upstream application and local-backend defaults ---
ARG OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui:v0.13.4-slim
FROM ${OPEN_WEBUI_IMAGE}

# Disable the unused API backend, automatic online fetches and telemetry.
# These are application settings; network routing is defined in compose.yml.
ENV OLLAMA_BASE_URL=http://ollama-offline:11434 \
    ENABLE_OLLAMA_API=true \
    ENABLE_OPENAI_API=false \
    OFFLINE_MODE=true \
    HF_HUB_OFFLINE=1 \
    DO_NOT_TRACK=true \
    SCARF_NO_ANALYTICS=true \
    ANONYMIZED_TELEMETRY=false

# --- Preserve upstream initialization, database migrations and startup ---
EXPOSE 8080
# Keep Open WebUI's official startup command and persistent data layout.
