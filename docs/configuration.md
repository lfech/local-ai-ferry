# Configuration and WebUI

[Project overview](../README.md)

Run commands from the project directory unless a step says otherwise.

## Services and storage

| Service | Image directory | Runtime role |
| --- | --- | --- |
| `ollama-online` | [ollama-online](../python-package/src/loaf/data/docker/ollama-online/Dockerfile) | Model downloads and exports in the download profile |
| `ollama-offline` | [ollama-offline](../python-package/src/loaf/data/docker/ollama-offline/Dockerfile) | Startup imports and inference in the offline profile |
| `open-webui` | [open-webui](../python-package/src/loaf/data/docker/open-webui/Dockerfile) | Browser interface; offline profile |
| `ollama-connected` | Same image as `ollama-offline` | Startup imports and inference with outbound internet access |
| `open-webui-online` | Same image as `open-webui` | Browser interface with outbound internet access |
| `terminal-server` | [terminal-server](../python-package/src/loaf/data/docker/terminal-server/Dockerfile) | Optional per-user Open Terminal provisioner. [Setup](terminal-server.md) |

All services have `scale: 1`. In the default project, the Ollama services mount the `ollama`
volume at `/ollama`, with `OLLAMA_MODELS=/ollama/models`. Its `models/` directory
contains weights and manifests, including newer Ollama catalog entries under
`models/manifests-v2/`. The volume's `config/` contains exported text Modelfiles.
The `open-webui` volume mounts at `/app/backend/data` for accounts, chats,
settings and caches. Stopping containers preserves both volumes.
Versioned deployments use separate volume names with the same mount paths.
[Versioned deployments](launcher.md#run-a-versioned-deployment)

## Project configuration

The Python package ships [default settings](../python-package/src/loaf/data/config/config.ini)
and the [default model list](../python-package/src/loaf/data/config/models.ini).
The checkout has editable copies in [config/](../config/). Its working
`config.ini` enables the terminal server. The packaged settings default to disabled.

`loaf` loads the packaged `config.ini`, then applies keys from the project's
`config/config.ini`. Keys you omit inherit their packaged values. A local
`models.ini` supplies the full model list. If it is absent, `loaf` uses the
packaged list. Pass `loaf --config-dir PATH` to select a different override
directory. `loaf` validates settings before Docker starts.

In a linked workspace with no local `config/`, copy the defaults to customize them.

```bash
loaf link-docker-project
mkdir config
cp docker-project/config/config.ini docker-project/config/models.ini config/
```

This example selects a separate deployment, a different model list and port,
and an external backup directory.

```ini
[launcher]
version = v2
models_file = model-examples/models.8gb.ini
wait_timeout = 600

[webui]
port = 3001

[transfer]
backup_dir = /media/transfer
```

Use bare values and put comments on separate lines. Paths are literal.
For a relative `models_file`, `loaf` checks the override directory and then the
packaged configuration directory. Leave it blank to select `models.ini`.
Relative `backup_dir` paths start at the project directory. Relative CLI paths
start at the current directory. [Model selection syntax](launcher.md#select-models)

Use `[terminal_server]` for the optional Open Terminal service. Its
`secret_file` path is relative to the project directory. Set
`include_in_backup = true` to transfer its user workspaces and key with
the standard backup. Backup includes the matching local terminal image when
available. Use `loaf backup --no-terminal-server` to skip that image.
Set `[images] terminal_server_image` to override its
base image. The former `[terminal_server] base_image` still works when no
replacement is set in the same file. Configure a custom Debian package mirror
for the terminal image build with `[terminal_server] apt_source_file`.
[Terminal server setup](terminal-server.md#enable-and-connect) · [Package mirror](terminal-server.md#package-mirror-for-image-builds)

### Overrides

- CLI options override the matching INI values. Use `--no-export` and
  `--no-import` to cancel configured transfer steps for one run. Backup
  still checks saved Modelfiles with either `export_models` setting. Use
  `--version=` to select the unversioned deployment.
  [Modelfile checks](transfer.md#check-saved-modelfiles-during-backup)
- The four existing Compose variables `OLLAMA_IMAGE`, `OPEN_WEBUI_IMAGE`,
  `OLLAMA_CONTEXT_LENGTH`, and `OLLAMA_STARTUP_TIMEOUT` still work. Compose
  checks the shell before the working project's `.env`. An empty or missing
  effective value falls back to the INI setting. Direct Compose commands run
  from `docker-project/` use the shell environment, `.env` in that directory,
  and Compose defaults. [Compose interpolation](https://docs.docker.com/reference/compose-file/interpolation/)
- For GPU selection, an exported `CUDA_VISIBLE_DEVICES` takes precedence over
  `cuda_visible_devices`. When the INI setting is blank, Compose can also read
  `CUDA_VISIBLE_DEVICES` from `.env`. Use comma-separated NVIDIA device IDs or
  UUIDs. [Ollama GPU selection](https://docs.ollama.com/gpu#gpu-selection)
- For `loaf test`, an exported `RUN_DOCKER_TESTS`, `RUN_GPU_TESTS`,
  `RUN_CATALOG_TESTS`, `RUN_TERMINAL_SERVER_TESTS`, or `ALLOW_LEGACY_TERMINAL_BRIDGE`
  overrides its matching INI key. The value `1` enables the option. The
  GPU test also requires the Docker check. The legacy bridge option runs the
  terminal-server test with ordinary internal bridges on Docker Engine 26 or 27.
  [Terminal server acceptance checks](terminal-server.md#acceptance-checks)

### Applying changes

The image settings select the base images used by `loaf build` and
`loaf upgrade`. Use fixed tags or digests for reproducible builds. Run the
desired serving command after changing runtime settings so Compose can
recreate the affected containers. Changing the default `version` selects
different images and volumes. Stop a previous deployment by selecting its
version explicitly. Deployments can use separate host ports while sharing the
same internal WebUI port.

`context_length` sets the Ollama server's default. A saved model parameter
`num_ctx` or a per-request setting from WebUI can override it. A larger context
uses more memory. `startup_timeout` bounds the Ollama API wait before
import. `wait_timeout` covers Compose readiness, including model imports.
To apply changed Modelfiles, run `loaf stop` and then start the desired serving
mode. Use `loaf stop -n` if you need to retain the stopped containers' logs.

The archive includes the project's `config.ini`. Backups made from a
checkout also include its source files and Git history. Keep credentials in
the appropriate application or host settings. Docker image tag namespaces,
volume mount paths, and offline network isolation are managed by the launcher.
Use `--archive` to choose a specific backup folder for one invocation.

## WebUI settings

Open the address printed by `loaf online` or `loaf offline`. The default is
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

### Run without signing in

`auth = true` is the default. To run without sign-in, set `auth = false` under
`[webui]` in `config.ini` and run `loaf offline` or `loaf online`. This sets
`WEBUI_AUTH=false` when Compose creates the WebUI container.

Choose the authentication mode before creating users in a fresh WebUI volume.
A separate versioned deployment gives you a fresh volume for single-user mode.
Anyone who can reach the instance can use it, so keep the default loopback
binding for private local use. [Open WebUI single-user mode](https://docs.openwebui.com/getting-started/quick-start/#single-user-mode-no-login) · [Versioned deployments](launcher.md#run-a-versioned-deployment)

## Networking and offline operation

The downloader uses a bridge with internet access. In `loaf online`, both Ollama
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

Basic chat uses the downloaded Ollama model. Custom embeddings, speech models,
tools, Python requirements and document features may need additional caches or
packages prepared before disconnection. See the
[Open WebUI offline guide](https://docs.openwebui.com/tutorials/maintenance/offline-mode/).
Image input is a separate functional check even when a vision projector is
present in the model.
