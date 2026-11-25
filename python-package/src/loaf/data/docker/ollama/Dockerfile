# Ollama downloader and serving images. BuildKit builds only the selected runtime.
ARG OLLAMA_IMAGE=ollama/ollama:latest
ARG OLLAMA_SOURCE=ollama/ollama:0.40.18
FROM ${OLLAMA_SOURCE} AS upstream

FROM debian:13-slim AS loaf-ollama-cpu
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
# Keep the CPU executable, its CPU libraries and the accompanying notices.
COPY --from=upstream /usr/bin/ollama /usr/bin/ollama
COPY --from=upstream /usr/lib/ollama/lib*.so* /usr/lib/ollama/*LICENSE* \
    /usr/lib/ollama/*NOTICE* /usr/lib/ollama/llama-server /usr/lib/ollama/llama-quantize /usr/lib/ollama/
ENV LD_LIBRARY_PATH=/usr/lib/ollama
ENTRYPOINT ["/usr/bin/ollama"]
CMD ["serve"]

FROM loaf-ollama-cpu AS loaf-ollama-cuda13
COPY --from=upstream /usr/lib/ollama/cuda_v13 /usr/lib/ollama/cuda_v13

# OLLAMA_IMAGE selects a bundled stage above or an external base image.
FROM ${OLLAMA_IMAGE} AS runtime
ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/ollama/models \
    OLLAMA_NOPRUNE=true
EXPOSE 11434

FROM runtime AS ollama-online
RUN apt-get update \
    && apt-get install -y --no-install-recommends rsync \
    && rm -rf /var/lib/apt/lists/*
COPY export-modelfiles.sh /usr/local/bin/export-modelfiles
RUN sed -i 's/\r$//' /usr/local/bin/export-modelfiles \
    && chmod 0755 /usr/local/bin/export-modelfiles \
    && mkdir -p /ollama/models /ollama/config
# Inherit the Ollama entrypoint and keep the shared model store in the volume.
CMD ["serve"]

FROM runtime AS ollama-offline
ENV OLLAMA_CONTEXT_LENGTH=16384 \
    OLLAMA_NO_CLOUD=1
COPY start-ollama-offline.sh /usr/local/bin/start-ollama-offline
RUN sed -i 's/\r$//' /usr/local/bin/start-ollama-offline \
    && chmod 0755 /usr/local/bin/start-ollama-offline \
    && mkdir -p /ollama/models /ollama/config
ENTRYPOINT ["/usr/local/bin/start-ollama-offline"]
CMD []
