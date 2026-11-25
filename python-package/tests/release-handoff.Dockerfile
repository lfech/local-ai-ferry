FROM docker:28.5.2-dind AS docker

FROM debian:13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends iptables iproute2 python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker /usr/local/bin/ /usr/local/bin/
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
WORKDIR /work
ENTRYPOINT ["dockerd"]
