# Configuration and WebUI

[Project overview](../README.md)

Run commands from the project directory unless a step says otherwise.

## Services and storage

| Service | Image directory | Runtime role |
| --- | --- | --- |
| `ollama-online` | [ollama](../python-package/src/loaf/data/docker/ollama/), stage `ollama-online` | Model downloads and exports in the download profile |
| `ollama-offline` | [ollama](../python-package/src/loaf/data/docker/ollama/), stage `ollama-offline` | Startup imports and inference in the offline profile |
| `open-webui` | [open-webui](../python-package/src/loaf/data/docker/open-webui/Dockerfile) | Browser interface; offline profile |
| `ollama-connected` | Same image as `ollama-offline` | Startup imports and inference with outbound internet access |
| `open-webui-online` | Same image as `open-webui` | Browser interface with outbound internet access |
| `terminal-server` | [terminal-server](../python-package/src/loaf/data/docker/terminal-server/Dockerfile) | Optional per-user Open Terminal provisioner. [Setup](terminal-server.md) |

All services have `scale: 1`. In the default project `loaf`, the Ollama services mount the `loaf-ollama`
volume at `/ollama`, with `OLLAMA_MODELS=/ollama/models`. Its `models/` directory
contains weights and manifests, including newer Ollama catalog entries under
`models/manifests-v2/`. The volume's `config/` contains exported text Modelfiles.
The `loaf-open-webui` volume mounts at `/app/backend/data` for accounts, chats,
settings, caches and the key that signs sign-in sessions. Stopping containers
preserves both volumes. The `loaf-terminal-users` volume holds each account's
`/home/user` folder of the [terminal server](terminal-server.md#offline-transfer-and-shutdown).
Separate namespaces use separate volume names such as `loaf-v1-2-3-ollama`
with the same mount paths. [Separate namespaces](launcher.md#run-a-separate-namespace) ·
[Docker resources](cli-help.md#docker)

## Project configuration

The Python package ships [default settings](../python-package/src/loaf/data/config/config.ini)
and the [default model list](../python-package/src/loaf/data/config/models.ini).
The checkout has editable copies in [config/](../config/). Its working
`config.ini` enables the terminal server. The packaged settings default to disabled.

Settings come in three layers. Later layers override individual keys of
earlier ones, so the project has the final say:

1. the packaged defaults,
2. the system settings in `/etc/loaf/config.ini`,
3. the project's `config/config.ini`.

Only a project's `config.ini` can move the system layer with `[launcher]
system_config = PATH`. Set it blank to skip the system settings. The model
list comes from the highest layer that has `models.ini`: the project, then
`/etc/loaf`, then the packaged list. Pass `loaf --config-dir PATH` to select
a different project directory. `loaf` validates every layer before Docker
starts. `loaf status` lists the settings files in use.
[System deployment](installation.md#debian-package)

`loaf setup` creates the project's settings when they are missing and never
replaces an existing file. It writes a `config.ini` with every key commented
out, so system settings and later packaged defaults stay in effect, and a copy
of the model list in effect, from `/etc/loaf` or else the packaged one. With
`--config-dir PATH`, setup writes both into that directory. A copied
`models.ini` keeps its selection when `/etc/loaf/models.ini` changes later. When
`/etc/loaf` has settings and the project uses the same namespace, both
operate the same Docker containers and volumes. `setup` and `status` warn
about this. Set `[launcher] namespace` in the project for a separate
deployment. A `[launcher] version` line from an earlier release fails with a
pointer to its new name. Tests replace the system directory with `LOAF_SYSTEM_CONFIG_DIR`.

A workspace from an earlier release with only a `docker-project` link has no
settings: run `loaf setup config` to create them, and `loaf clean --full` to
remove the unused link.

This example selects a separate deployment, a different model list and port,
and an external backup directory.

```ini
[launcher]
namespace = v2
models_file = model-examples/models.8gb.ini
wait_timeout = 600

[webui]
port = 3001

[transfer]
backup_dir = /media/transfer
```

Use bare values and put comments on separate lines. Paths are literal.
`wait_timeout = 0` waits for readiness without a limit.
For a relative `models_file`, `loaf` checks the override directory and then the
packaged configuration directory. Leave it blank to select `models.ini`.
Relative `backup_dir` paths start at the project directory. Relative CLI paths
start at the current directory. [Model selection syntax](launcher.md#select-models)

`[transfer] backup_version_min` names the oldest archive format that restore
accepts. This release writes and reads format 3, so 3 is the only valid
value. Archives from earlier releases need the launcher in their backup
folder. [Archive contents](transfer.md#archive-contents)

Use `[terminal_server]` for the optional Open Terminal service. Its
`secret_file` path is relative to the project directory. With the terminal
server enabled, backups without type names include the `terminal` type while
`include_in_backup` is true, the default. It holds the user volume with every
account's folder and the bearer key, which the WebUI's saved connection
needs. Backup includes the matching local terminal image when available. Use
`loaf backup --no-terminal-server` to leave out the image, the user volume
and the key; restoring that backup disables the terminal server.
Set `[images] terminal_server_image` to override its
base image. Set `[terminal_server] apt_source_file` for the terminal image
build and for `apt` in chat containers. Set `apt_repository_dir` for a
repository stored on the Docker host. `network = connected` lets chat
containers reach every address the Docker host reaches; the default
`isolated` keeps them on their own bridge.
[Terminal server setup](terminal-server.md#enable-and-connect) · [Package mirror](terminal-server.md#package-mirror-for-image-builds)
[Install packages in a chat](terminal-server.md#install-packages-in-a-chat) · [Network access](terminal-server.md#network-access)

### Select an Ollama build recipe

Set `[images] ollama_image` in `config/config.ini`, then run `loaf build`
on the connected preparation host. The packaged Compose project builds
the selected recipe for both the downloader and the inference backend.
The same values work with the `OLLAMA_IMAGE` override for direct Compose use.

| `ollama_image` | Runtime |
| --- | --- |
| `loaf-ollama-cuda13` | Default. CPU and CUDA 13. GPU serving requires NVIDIA driver 580 or newer and a working container runtime. |
| `loaf-ollama-cuda12` | CPU and CUDA 12 for NVIDIA drivers from 550 to 579. GPUs with compute capability 5.0 to 6.2 need driver 570 or newer. Debian 13's own driver package qualifies. |
| `loaf-ollama-cpu` | CPU only. Serve with `loaf run offline --cpu` or `loaf run online --cpu`. |
| `ollama/ollama:latest` | Full upstream Ollama image with CUDA 12 and 13 and the same driver requirements. Other image references also work. |

The compact recipes copy runtime files from Ollama 0.40.18. The first
build can fetch the full upstream source image. The annotated sizes in
`config.ini` refer to the resulting images. `loaf upgrade` refreshes the
images used by the selected recipe.
[Ollama Dockerfile](../python-package/src/loaf/data/docker/ollama/Dockerfile) ·
[CUDA driver requirements](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html) ·
[Ollama NVIDIA requirements](https://docs.ollama.com/gpu) ·
[Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/)

### Select the WebUI image

The default `webui_image` is Open WebUI v0.13.4 slim. It supports accounts,
chats and Ollama models with the default SQLite database. It omits local
embeddings, speech and PDF or Office document processing. Its retrieval
features need PostgreSQL with pgvector. Select
`ghcr.io/open-webui/open-webui:main` for the full image before preparing a
host that needs these features offline.
[Open WebUI image variants](https://docs.openwebui.com/getting-started/quick-start/#image-variants)

### Overrides

- CLI options override the matching INI values. Use `--no-export` and
  `--no-import` to cancel configured transfer steps for one run. Backup
  still checks saved Modelfiles with either `export_models` setting. Use
  `--namespace=` to select the default namespace `loaf`.
  [Modelfile checks](transfer.md#check-saved-modelfiles-during-backup)
- The four existing Compose variables `OLLAMA_IMAGE`, `OPEN_WEBUI_IMAGE`,
  `OLLAMA_CONTEXT_LENGTH`, and `OLLAMA_STARTUP_TIMEOUT` still work. Compose
  checks the shell before the working project's `.env`. An empty or missing
  effective value falls back to the INI setting. [Compose interpolation](https://docs.docker.com/reference/compose-file/interpolation/)
- For GPU selection, an exported `CUDA_VISIBLE_DEVICES` takes precedence over
  `cuda_visible_devices`. When the INI setting is blank, Compose can also read
  `CUDA_VISIBLE_DEVICES` from `.env`. Use comma-separated NVIDIA device IDs or
  UUIDs. [Ollama GPU selection](https://docs.ollama.com/gpu#gpu-selection)
- For `loaf test`, an exported `RUN_DOCKER_TESTS`, `RUN_GPU_TESTS`, `RUN_CPU_TESTS`,
  `RUN_CATALOG_TESTS`, `RUN_TERMINAL_SERVER_TESTS`, or `ALLOW_LEGACY_TERMINAL_BRIDGE`
  overrides its matching INI key. The value `1` enables the option. The
  CPU and GPU model workflows also require the Docker check. The legacy bridge option runs the
  terminal-server test with ordinary internal bridges on Docker Engine 26 or 27.
  [Terminal server acceptance checks](terminal-server.md#acceptance-checks)

### Applying changes

The image settings select the base images used by `loaf build` and
`loaf upgrade`. Use fixed tags or digests for reproducible builds. Run the
desired serving command after changing runtime settings so Compose can
recreate the affected containers. Changing the default `namespace` selects
different images and volumes. Stop a previous deployment by selecting its
namespace explicitly. Deployments can use separate host ports while sharing the
same internal WebUI port.

`context_length` sets the Ollama server's default. A saved model parameter
`num_ctx` or a per-request setting from WebUI can override it. A larger context
uses more memory. `startup_timeout` bounds the Ollama API wait before
import. `wait_timeout` covers Compose readiness, including model imports.
To apply changed Modelfiles, run `loaf stop` and then start the desired serving
mode. Use `loaf stop --no-remove-containers` if you need to retain the stopped
containers' logs.

The archive includes the project's `config.ini`. Backups made from a
checkout also include its source files and Git history. Keep credentials in
the appropriate application or host settings. Docker image tag namespaces,
volume mount paths, and offline network isolation are managed by the launcher.
Use `--archive` to choose a specific backup folder or archive file for one
restore.

## WebUI settings

Open the address printed by `loaf run online` or `loaf run offline`. The default is
**http://localhost:3000**. With authentication enabled, create the first admin
account or sign in. Check the Ollama connection and select your model.

| Setting | Starting value |
| --- | --- |
| Ollama URL | `http://ollama-offline:11434` |
| Model | Full `hf.co/...` name from your list, or an existing local alias |
| Context length (`num_ctx`) | Explicitly `16384` for the Qwen3 examples |
| Maximum output tokens | Initially `4096`, including reasoning |
| Temperature | Upstream defaults, or `0.6` for Qwen3 / `0.7` for Ministral reasoning |

Connection settings are in the admin area. Model and chat Advanced Params hold
context and sampling settings. Saved WebUI preferences can override environment
defaults.

Ministral without a saved context override uses the configured server context.
Qwen3's published presets request 40960 context tokens and 32768 output tokens;
explicitly override them for the memory estimates in [the model guide](models.md).
See [Open WebUI's context settings](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-ollama/#context-length-num_ctx-and-ollama_context_length).

Enable the optional tool history filter in WebUI's model settings when an
Ollama model rejects a follow-up after tool results.
[Conversation roles must alternate](troubleshooting.md#conversation-roles-must-alternate)

Open WebUI signs sign-in sessions with a key that loaf keeps in the WebUI
volume as `.webui_secret_key`, through Open WebUI's `WEBUI_SECRET_KEY_FILE`.
Sign-ins therefore survive new containers, switches between the `online` and
`offline` profiles, and a restore of the `webui` type on another host. Open WebUI
creates the key on its first start. Outside its volume, Open WebUI writes only
Python bytecode, which needs no backup.

### Run without signing in

`auth = true` is the default. To run without sign-in, set `auth = false` under
`[webui]` in `config.ini` and run `loaf run offline` or `loaf run online`. This sets
`WEBUI_AUTH=false` when Compose creates the WebUI container.

Choose the authentication mode before creating users in a fresh WebUI volume.
A separate namespace gives you a fresh volume for single-user mode.
Anyone who can reach an instance without sign-in can use it. Keep the default
loopback binding and check the Docker version's loopback behavior before using
this mode on a connected host. [Open WebUI single-user mode](https://docs.openwebui.com/getting-started/quick-start/#single-user-mode-no-login) ·
[Docker loopback publishing](local-network.md#remove-local-network-publishing)

## Networking and offline operation

The downloader uses a bridge with internet access. In `loaf run online`, both Ollama
and WebUI use the `connected` bridge. By default, `ollama_cloud` and
`webui_downloads` are enabled in the online profile. Turning off
`webui_downloads` enables WebUI's offline settings for automatic downloads
and update checks while keeping its network connection.
Offline Ollama uses only the internal inference network. No Ollama service
publishes a host port.
By default, the serving backends request NVIDIA devices through `gpus: all`
and a device reservation for older Compose releases. The host NVIDIA driver
and container runtime must already work. With `--cpu`, the launcher adds the
packaged `compose.cpu.yml` override, which removes the device requests and
hides CUDA devices. [Compose compatibility](debian.md#prepare-debian-before-disconnecting-it) · [CPU serving](launcher.md#serve-on-cpu)

Both WebUI services can reach the terminal server on the internal
`terminal-control` network. The terminal server publishes no host port.
On first terminal use, it creates a separate runner and network for the
account and chat. Its read-only WebUI data mount supplies saved chat ownership
and deletion checks. [Workspace isolation](terminal-server.md#workspace-policy)

WebUI connects to Ollama at the same URL in both modes, including connections
saved to its database. Offline WebUI uses a regular `web` bridge for
localhost publishing. The default host binding is `127.0.0.1:3000` and the
container port is `8080`. Set `[webui]` `bind_address` to an IP address on
the host and `port` to a free port. For remote access through the default
binding, use an [SSH forward](debian.md#verify-and-open-webui).
For direct access from other devices, see the [local network guide](local-network.md).

WebUI's offline settings suppress automatic downloads and update checks.
Both serving profiles inherit the image defaults that disable OpenAI connections
and opt out of telemetry. Offline WebUI's bridge can still provide outbound
access while the host is connected. Physical disconnection isolates the
computer from external networks.

## Extra features

Basic chat uses the downloaded Ollama model. The default slim WebUI image
has no local embedding, speech or document processing.
[WebUI image](#select-the-webui-image)
With the full image, custom embeddings, speech models, tools, Python
requirements and document features may need additional caches or packages
prepared before disconnection. See the
[Open WebUI offline guide](https://docs.openwebui.com/tutorials/maintenance/offline-mode/).
Image input is a separate functional check even when a vision projector is
present in the model.
