# Local AI Ferry

`loaf` prepares Ollama models, serves them through Open WebUI using Docker
Compose, and transfers deployments to offline Debian hosts.

After installing the wheel into a virtual environment, run `loaf setup` in
your deployment directory and use `loaf --help` for available commands. Use the source checkout to prepare a
host and back up the full checkout. The standalone release executable can
also prepare, back up and restore a deployment.

[Project documentation](https://github.com/lfech/local-ai-ferry)
