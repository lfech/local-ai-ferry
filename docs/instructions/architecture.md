# Architecture

## Deployment

The `loaf` launcher prepares Hugging Face models and serves them with Ollama
and Open WebUI on connected or disconnected hosts. The documented hosts are
native Debian and Debian in WSL2 with Docker Engine. The launcher uses the
Python 3.10+ standard library. GPU inference requires working NVIDIA host
drivers and container support. Compose 2.30 and newer support `gpus: all`.
Compose 2.26.1 through 2.29.x use the NVIDIA device reservation in the same
service definition. [Installation](../installation.md) ·
[Docker and Compose requirements](../debian.md#prepare-debian-before-disconnecting-it)

## CLI and Compose profiles

The Python package ships the [deployment data](../../python-package/src/loaf/data/)
with one Compose file, three core build contexts, the optional terminal server
context and the default configuration. [Terminal server](../terminal-server.md)
`loaf link-docker-project` exposes the deployment tree for direct Compose calls.
[Direct Compose commands](../launcher.md#link-the-docker-project)

| Command | Behavior |
| --- | --- |
| `loaf setup` / `loaf install` | Standalone setup writes default user configuration and links the packaged deployment. Source checkout setup prepares the Python wheel cache, virtualenv and NVIDIA Container Toolkit on Debian. On Debian 13, `--with-docker-deps` also prepares native Docker packages. [Quick Setup](../../README.md#quick-setup) |
| `loaf binary` | Build or update the standalone Linux amd64 executable at `dist/loaf` using the current source files. [Local binary build](../releases.md#build-and-check-locally) |
| `loaf clean` | Clear the user runtime cache and project temporaries. `--full` also clears generated installation files. Git, backups, settings, credentials and Docker data are preserved. [Cleanup](../launcher.md#clean-generated-files) |
| `loaf build` | Builds the downloader, GPU Ollama and Open WebUI images. Includes the terminal image when enabled. |
| `loaf upgrade` | Builds the same images with Compose's `--pull` option to refresh their base images. |
| `loaf download` | Stops serving services, starts the downloader in the `download` profile and pulls each selected model. |
| `loaf export` | Stops the downloader and archives the Ollama model store using a disposable container. |
| `loaf online` | Stops the downloader and offline services, then starts GPU Ollama and WebUI on the connected bridge. |
| `loaf import` | Stops the downloader and synchronizes the archive into the Ollama volume. Supports optional deletion of destination-only data. |
| `loaf offline` | Stops the downloader and online services, then starts GPU Ollama and WebUI from local images. |
| `loaf backup` / `loaf restore` | Create or recover a deployment bundle with selectable images, volumes and project components. Use `--models-only` for model-store transfer. |
| `loaf status` | Checks deployment readiness and lists the project's containers. [Readiness checks](../launcher.md#check-readiness-with-status) |
| `loaf stop` | Stops services and removes their Compose containers by default. Use `-n` or `--no-remove-containers` to keep stopped service containers and their logs. Images and volumes persist. [Shutdown options](../launcher.md#stop-and-remove-containers) |
| `loaf integration <name>` | Prints a systemd unit, Continue configuration or Tether installation instructions. [Host integrations](../installation.md#host-integrations) |
| `loaf test [-v]` | Runs the selected project's unittest suite. [Verification guide](../verification.md) |

Both `loaf` and `python -m loaf` use the current directory by default.
`--project-dir` selects another checkout or linked directory. The checkout has
an editable `config/`. Local settings override packaged keys, and a local
model file replaces the selected bundled list. Use
`--config-dir` to select a different override directory.
Download and `backup --export` use the selected model INI. Named sections
replace `[models.default]`. An optional `name` creates an Ollama alias. [Model selection](../launcher.md#select-models)

The launcher holds an OS lock across Compose transitions and transfers in the
checkout. The serving and build plans validate the Compose configuration
before changing services or building images. Status and archive verification
are observational. Execution stops at the first failure and preserves completed
downloads and exports.
`--dry-run` describes the steps without invoking Docker.
`-v` prints the Docker Compose commands for live runs and previews.
`--attach` follows Compose logs after the workflow releases the lock.
`--wait-timeout` controls the Compose readiness wait and defaults to 300
seconds. [Launcher options and failure handling](../launcher.md)

## Model storage and serving

The default Compose project is `local-inference`, with local images tagged
`latest` and named volumes `ollama` and `open-webui`. A versioned project
scopes container and network names through Compose and uses matching image
tags and explicit volume names. The transfer commands use the same namespace
when naming archives and checking restored images.
[Versioned deployments](../launcher.md#run-a-versioned-deployment)

All three Ollama services mount the `ollama` volume at `/ollama`. Its
`models/` directory holds manifests and blobs, including vision projectors
where supplied. The export helper atomically writes complete Modelfiles under
`config/`, preserving model names, tags, templates and blob references.
Changing the selection leaves existing exports and registrations in place.
[Storage layout](../configuration.md#services-and-storage)

Both serving profiles use the same GPU Ollama image and startup wrapper. The
wrapper waits for the local API, then recursively imports every saved
`.model` file. With no exports, Ollama serves existing registrations. An
import failure blocks backend readiness, and WebUI waits for a healthy
backend. The GPU server defaults to a 16,384-token context and a 60-second API
startup wait. [Startup and configuration](../configuration.md#applying-changes)

Both serving profiles use the same WebUI image and persist accounts, chats and
settings in the `open-webui` volume. WebUI reaches Ollama at
`http://ollama-offline:11434` in both modes. The default browser address is
`http://localhost:3000`. Ollama has no published host port. The online pair
runs on a bridge with outbound internet access. Its default settings enable
Ollama cloud features and automatic WebUI downloads.
Offline Ollama runs only on an internal Docker network. Offline WebUI also
uses a regular bridge for localhost publishing, which can provide outbound
access while the host is connected. The offline image settings suppress
automatic downloads and telemetry. Disconnecting the host isolates the entire
deployment. [Networking and offline operation](../configuration.md#networking-and-offline-operation)

## Open Terminal workspaces

When `[terminal_server] enabled = true`, both serving profiles start the
Open Terminal provisioner. Open WebUI reaches it at
`http://terminal-server:8000` over the internal `terminal-control` network.
WebUI supplies the authenticated account and, when available, the chat
session. The provisioner authenticates the connection with the terminal bearer
key and routes file requests, model commands and terminal streams to the
corresponding runner. [Terminal connection](../terminal-server.md#enable-and-connect)

Compose mounts the Docker socket in the provisioner so it can create a
container and internal bridge for each workspace. Draft requests use the
account's runner. Each chat session has a separate runner for that account.
Runners start from the provisioner's image or the current chat's locally
derived package image. They run as an unprivileged user and have a
read-only root filesystem, a bounded temporary filesystem at
`/tmp`, and CPU and memory limits. Each runner's API stays on its own bridge.
Docker Engine 26 and 27 use `inhibit_ipv4` to keep an IPv4 address off the
host bridge. Docker Engine 28 and newer use the `isolated` gateway mode.
[Workspace isolation and limits](../terminal-server.md#workspace-policy)

With a configured APT source, Open WebUI can request packages for an
authenticated chat. The provisioner runs APT in a disposable installer
from that chat's image with access to the configured mirror and without
the runner's data volumes or credentials. It publishes an image for the
chat after APT succeeds. The new runner uses that image with either
working directory. [Chat packages](../terminal-server.md#install-packages-for-a-chat)

| Folder | Storage and lifetime |
| --- | --- |
| `/home/user` | Persistent named volume per account. All of the account's workspaces mount it. |
| `/home/chat` in a draft | Empty, read-only placeholder that the user can select as the working directory before sending the first message. |
| `/home/chat` in a saved chat | Writable named volume provisioned on first terminal use. It survives runner replacement, service shutdown and chat archival. |
| `/home/chat` in a temporary chat | Writable temporary filesystem for the lifetime of the runner. |

The file browser root is `/home` and the default working directory is
`/home/user`. The provisioner supplies `user` and `chat` as direct entries
in Open WebUI's **Working Directory** menu alongside the account's folder
history. [Files and working directory](../terminal-server.md#use-the-terminal-from-a-model)

The bundled deployment mounts the existing `open-webui` volume read-only in
the provisioner. It checks saved chat ownership and existence in the WebUI
SQLite database. After WebUI deletes a chat, the next successful cleanup pass
removes its runner, network, chat volume and package image. Idle cleanup and provisioner
shutdown remove runners and their networks while preserving named volumes.
[Workspace lifecycle](../terminal-server.md#workspace-policy)

## Backup and restore

`loaf backup volumes --models-only` and `loaf restore volumes --models-only`
use the same model-store format and implementation as `loaf export` and
`loaf import`. Backup stages the store with `rsync` before compressing it.
Restore uses `rsync` to publish blobs before manifests. The default merges
with the destination. Add `--delete` to mirror the archive. Both operations
stop the downloader and allow the selected Ollama serving container to keep
running. [Model transfer](../transfer.md#transfer-models-between-existing-deployments)

`loaf backup` publishes one `YYYYMMDD-HHMM_local-ai-ferry/` folder per
successful run in the default deployment. The folder has the matching
`.tar.gz` archive, the standalone `loaf` executable, and `backup.json` with
the archive name, deployment version and executable SHA-256 hash. An
operator can select any nonempty subset of `images`, `volumes` and `project`.
Without component names, backup includes all three and restore recovers
all available components. The archive uses the following members.

| Member | Contents |
| --- | --- |
| `volumes.tar` | The `ollama` and `open-webui` volumes and the persistent per-account terminal volumes when included. |
| `images.tar` | The three core Docker images and the matching Open Terminal image when available locally. Use `--no-terminal-server` to omit the terminal image. |
| `project.tar` | Configuration, saved Modelfiles and cached installers. Backups from source checkouts retain working files, Git history and the project virtualenv. |
| `terminal.json` | With `volumes`, the per-account volume inventory and bearer key when `[terminal_server] include_in_backup = true`. |
| `sha256.json` | The SHA-256 digest of each payload. |

Backup stops all project services before capturing the volumes and publishes
the folder only after all selected payloads are complete. The default output
directory is the Git-ignored `backup/` directory. Both commands use the
configured `backup_dir` and accept `--backup-dir` for one run. Restore selects
the folder it runs from, the folder containing its executable, or the
newest backup for the chosen namespace. Use `loaf restore --archive PATH`
to select a specific folder. To carry chat files
in a terminal backup, move them into `/home/user` before running it.
[Terminal backup contents](../terminal-server.md#offline-transfer-and-shutdown)

Backups from a source checkout build the current executable before capturing
Docker data. Standalone deployments package the executable that launched them.

`loaf backup --verify` checks the executable and all payload hashes and
reports platforms when images are included. Restore verifies the entire
archive and rejects explicitly requested components that are absent. It
loads selected images, extracts selected volumes into new or empty volumes
and recovers settings and Modelfiles when `project` is selected. A volume
restore uses the matching local downloader image when images are omitted.
It refuses volumes in use and restores the archived terminal key with the
terminal volumes.
By default, restore leaves services stopped. Add `--import` to start offline
Ollama and apply saved Modelfiles after restoring volumes. The optional backup
`--export` step refreshes selected cached Modelfiles before capturing volumes. Both
options run under the transfer's project lock.
A prepared compatible host can restore and start offline without network
access. [Backup and restore](../transfer.md)

The release and backup executables use the same setup workflow. They
supply Python and the packaged deployment files. `loaf setup` creates the
default configuration and Docker link. `loaf restore` is optional and uses
a deployment backup. The separate source archive contains the tagged repository
files. [Release assets](../releases.md)
Setup stores Python wheels in `install-cache/wheels/` and matching toolkit
installers in `install-cache/deb/nvidia/`. On Debian 13, the optional Docker
cache contains native Engine, CLI, Compose, Buildx and required packages
under `install-cache/deb/docker/` with a local APT index. Each Docker cache
targets one Debian release and package architecture. Git ignores
`install-cache/`. [Debian installation](../installation.md#disconnected-debian-installation) ·
[Host integrations](../installation.md#host-integrations)
