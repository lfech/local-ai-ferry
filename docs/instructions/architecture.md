# Architecture

[Project overview](../../README.md) · [Configuration](../configuration.md) ·
[Verification](../verification.md)

## Deployment

The `loaf` launcher prepares Hugging Face models for Ollama and serves them
through Open WebUI on connected or disconnected hosts. The documented hosts
are native Debian and Debian in WSL2 with Docker Engine and Compose 2.26.1 or
newer. The launcher uses the Python 3.10+ standard library. The standalone
Linux amd64 executable supplies its own Python and deployment files.
[Installation](../installation.md) · [Release executable](../releases.md#run-the-executable)

Three channels deliver the same launcher: the repository for development,
the standalone executable for portable deployment directories, and the
Debian package for the system deployment. The release wheel and the Debian
package carry the standalone executable, which every backup folder includes. A deployment directory is the
current directory or `--project-dir`. The system deployment uses `/etc/loaf`
for settings and `/var/lib/loaf` for data, backups and the lock. The Debian
package selects it by default, and `--system` selects it from any channel.
Settings layer as packaged defaults, then `/etc/loaf`, then a project's
`config/config.ini`, and later layers override earlier ones. Only a project
can move or skip the system layer. Every channel carries the guides of its
release for `loaf docs`.
[Settings layers](../configuration.md#project-configuration) · [Debian package](../installation.md#debian-package)

## CLI and Compose profiles

The Python package ships the [deployment data](../../python-package/src/loaf/data/)
with one Compose file, the Ollama and Open WebUI build contexts, the optional
terminal server context and the default configuration. Both Ollama images
build from one context with a multi-stage Dockerfile for registry bases and
the bundled CPU, CUDA 12 and CUDA 13 recipes. Each Dockerfile and default build secret
stays inside its context, which Compose always allows Buildx to read.
[Ollama build selection](../configuration.md#select-an-ollama-build-recipe) ·
[Terminal server](../terminal-server.md)
`loaf setup` runs selectable parts like the types of `loaf backup`; `all`,
the default, holds the parts that the installation needs, and the others run
when named. A part keeps what exists unless `--force` replaces it, the flag
that also confirms removals in `clean --full` and `docker rm`.
[Setup parts](../cli-help.md#setup)

The [command reference](../cli-help.md) explains every command and option.
`loaf run offline` and `loaf run online` start a serving profile, `loaf stop`
stops one profile or, without one, every service, and `loaf restart` stops a
profile and starts it again with new containers.

Both `loaf` and `python -m loaf` use the current directory by default.
`--project-dir` selects another deployment. Every installation runs Compose
with its own deployment files: the source checkout's, the standalone
executable's runtime, the wheel's or the Debian package's. A project holds
only settings and data: a source checkout is one, and elsewhere the settings
that `setup` creates make one. Users reach the containers by their fixed
names with plain Docker commands. [Inspect the containers](../launcher.md#inspect-the-containers)
Local configuration overrides individual packaged keys. A local model INI
supplies the entire selected list. Use `--config-dir` to select a different
override directory.
Download and `backup --export` use the selected model INI. Named sections
replace `[models.default]`. An optional `name` creates an Ollama alias, and download then removes the long
source name unless another entry selects the source without a name. [Model selection](../launcher.md#select-models)

The launcher holds a per-directory OS lock across Compose transitions and
transfers. The serving and build plans validate Compose configuration before
changing services or building images. Status inspects local resources and
uses disposable, network-disabled probe containers. Archive verification
runs without Docker. Execution stops at the first failure and preserves
completed downloads and exports.
`--dry-run` describes the steps without invoking Docker.
`-v` prints the Docker Compose commands for live runs and previews.
`--attach` follows Compose logs after the workflow releases the lock.
`--wait-timeout` controls the Compose readiness wait and defaults to 300
seconds; `0` waits without a limit. Option names are not abbreviated.
[Launcher options and failure handling](../launcher.md)

## Model storage and serving

| Compose profile | Services | Networks |
| --- | --- | --- |
| `download` | `ollama-online` | Outbound `download` bridge. |
| `online` | `ollama-connected`, `open-webui-online` | Outbound `connected` bridge. WebUI also joins the internal `terminal-control` network. |
| `offline` | `ollama-offline`, `open-webui` | Internal `inference` network. WebUI also joins the regular `web` bridge and the internal `terminal-control` network. |
| `terminal` | `terminal-server` | Internal `terminal-control` network. The launcher starts the provisioner alongside either serving profile when enabled. |

The launcher stops services from competing profiles before switching the
shared volumes to the next profile. Startup uses local images with
`--no-build --pull never`. Each Compose service has `scale: 1`. The serving
services and the terminal server restart with Docker unless loaf stopped
them; the downloader never starts by itself.
[Compose definition](../../python-package/src/loaf/data/compose.yml)

Every Docker resource name starts with the deployment namespace, which is
also the Compose project: `loaf` by default and `loaf-NAME` for a deployment
selected with `--namespace NAME` or `[launcher] namespace`. Compose prefixes
container and network names with it,
and the volumes are `NAMESPACE-ollama` and `NAMESPACE-open-webui`. The
images are `loaf/ollama-online`, `loaf/ollama-offline`, `loaf/open-webui` and
`loaf/open-terminal`, tagged `latest` or the namespace name. The terminal
provisioner names its chat containers, bridges and user volume
`NAMESPACE-terminal-…` and labels them `loaf.terminal.*`. The transfer
commands use the namespace name when naming backup folders and checking
restored images. Volumes that loaf creates carry Compose's labels, so Compose uses
them without a warning.
[Separate namespaces](../launcher.md#run-a-separate-namespace) ·
[Docker resources](../cli-help.md#docker)

All three Ollama services mount the Compose volume `ollama`, named
`loaf-ollama` by default, at `/ollama`. Its
`models/` directory holds manifests and blobs, including vision projectors
where supplied. Ollama registrations can use `manifests/` or `manifests-v2/`,
where tags can link to content-addressed manifest blobs.
The export helper atomically writes complete Modelfiles under
`config/`, preserving model names, tags, templates and blob references.
Changing the selection leaves existing exports and registrations in place.
[Storage layout](../configuration.md#services-and-storage)

Both serving profiles use the same Ollama image and startup wrapper.
NVIDIA GPU inference is the default and requires a compatible host driver
and container runtime. The default CUDA 13 recipe needs driver 580 or newer,
and the CUDA 12 recipe needs 550 or newer. The Compose service definition supplies `gpus: all`
for Compose 2.30 and newer and a device reservation for 2.26.1 through 2.29.x.
With `--cpu`, the launcher applies `compose.cpu.yml` to remove the GPU
requests and hide GPU devices from Ollama. CPU serving uses the same image,
volumes and startup wrapper. [Docker and Compose requirements](../debian.md#prepare-debian-before-disconnecting-it) ·
[CPU serving](../launcher.md#serve-on-cpu)

The startup wrapper waits for the local API, then recursively imports saved
`.model` files from `/ollama/config`. With no exports, Ollama serves existing
registrations. An import failure blocks backend readiness, and WebUI waits
for a healthy backend. Ollama defaults to a 16,384-token context and a
60-second API startup wait. [Startup and configuration](../configuration.md#applying-changes)

Both serving profiles use the same WebUI image and persist accounts, chats and
settings in the `open-webui` volume. WebUI reaches Ollama at
`http://ollama-offline:11434` in both modes using an alias in the connected
profile. WebUI is the only service with a published host port. The default
browser address is `http://localhost:3000`, backed by `127.0.0.1:3000` on
the host and port `8080` in the container.

The online profile enables Ollama cloud features and automatic WebUI
downloads by default. Offline settings disable Ollama cloud features and
suppress automatic WebUI downloads. Both WebUI profiles opt out of telemetry.
Offline WebUI's regular bridge can still provide outbound access while the
host is connected. Physical disconnection isolates the host from external
networks. [Networking and offline operation](../configuration.md#networking-and-offline-operation)

## Open Terminal workspaces

Only `[terminal_server] enabled = true` turns the terminal server on; the
packaged and the checkout's settings leave it off, and the other
`[terminal_server]` settings, such as `network`, have no effect until then.
When enabled, the launcher requires WebUI authentication and validates the
configured bearer key before serving, also in dry runs.
Open WebUI reaches the provisioner at `http://terminal-server:8000` over
the internal `terminal-control` network. A Compose override mounts the bearer
key in both WebUI services, and the WebUI image's start script presets this
connection through Open WebUI's `TERMINAL_SERVER_CONNECTIONS`. Open WebUI
stores it while it has no terminal connections, for administrators or, with
`[terminal_server] webui_access = users`, for every account.
WebUI supplies the authenticated account and, when available, the chat
session. The provisioner authenticates the connection with the terminal bearer
key and routes file requests, model commands and terminal streams to the
corresponding runner. [Terminal connection](../terminal-server.md#enable-and-connect)

Compose mounts the Docker socket in the provisioner so it can create a
container and internal bridge for each chat. Draft requests use one container
per account. The account's own requests and the model's tool calls reach the
same container, and the provisioner publishes Open Terminal's tools at their
own paths. Containers start from the provisioner's image. Commands run as root
with only the capabilities that `apt`, `dpkg` and `sudo` need and with
`no-new-privileges`, so `apt` works as usual. Each container has a writable
root filesystem, a bounded temporary filesystem at `/tmp`, CPU and memory
limits and an unpublished API on its own internal bridge. Docker Engine 26 and
27 use `inhibit_ipv4` to keep an IPv4 address off the host bridge. Docker
Engine 28 and newer use the `isolated` gateway mode. `[terminal_server]
network` decides further access: `isolated` keeps containers on their bridge
alone, and `connected` also attaches them to a shared bridge with outbound
access and inter-container traffic disabled. `loaf run offline` prints a
note, not a warning, about that access.
[Workspace isolation and limits](../terminal-server.md#workspace-policy)

A configured `apt_source_file` replaces inherited sources during an image
build, and in chat containers at runtime. At startup, the launcher copies its
provisioner and runner scripts to `.loaf-terminal-runtime/` in the project,
a path that stays the same across loaf releases, and a Compose override
mounts them and the source file read-only. The provisioner mounts the source
file and the runner in every chat container, and an `apt_repository_dir`
read-only at `/run/loaf-apt/repository`. A container records the paths of
its runtime mounts, the repository and the network setting it was created
with; a change replaces it on the next start. Changed contents at the same
paths apply on its next start, so a new loaf release keeps chat containers.
[Package mirror](../terminal-server.md#package-mirror-for-image-builds) ·
[Install packages in a chat](../terminal-server.md#install-packages-in-a-chat)

| Folder | Storage and lifetime |
| --- | --- |
| `/home/user` | The account's folder in the deployment's single user volume, mounted as a volume subpath. A container sees no other account's folder. A short-lived container creates the folder on the account's first use. |
| `/home/chat` in a chat | Part of the chat's container, together with installed packages. An idle container stops and gives up its bridge, and both survive until the chat is deleted. A temporary chat's container is removed after `idle_seconds` and at provisioner shutdown. |
| `/home/chat` in a draft | Part of the account's draft container, not carried into the chat. |

The file browser root is `/home`. `HOME` is `/home/user`, so dotfiles persist
with the account. The default working directory is `/home/chat` in a chat and
`/home/user` in a draft. The provisioner supplies `user` and `chat` as direct
entries in Open WebUI's **Working Directory** menu alongside the account's
folder history. [Files and working directory](../terminal-server.md#use-the-terminal-from-a-model)

The bundled deployment mounts the existing `open-webui` volume read-only in
the provisioner. It checks saved chat ownership and existence in the WebUI
SQLite database. After WebUI deletes a chat, the next successful cleanup pass
removes its container and bridge. Before it stops an idle container, the
provisioner closes the account's disconnected terminal shells through Open
Terminal's `/api/terminals`, which would otherwise keep it busy. At shutdown,
the provisioner stops the containers of saved chats and drafts and removes
those of temporary chats.
Releases before v1.0.0 migrate no terminal data from earlier layouts.
[Workspace lifecycle](../terminal-server.md#workspace-policy)

## Backup and restore

`loaf backup` publishes one `YYYYMMDD-HHMM_local-ai-ferry/` folder per
successful run in the default deployment. The folder has the archive and the
standalone `loaf` launcher that can restore it; a folder is recognized by an
archive whose first member is `loaf.json`. The archive is a gzip-compressed
tar. `loaf.json` records the archive format, the namespace name, the
launcher's size and SHA-256 hash, and one item per type with each member's
path, size and SHA-256 hash. Each type adds one
plain tar member; `terminal` adds its volume and the bearer key as separate
members. These hashes reveal damage, not deliberate changes, so backup and
`backup --verify` print the SHA-256 of the archive and the launcher for a
comparison over another channel. A backup made with `--no-terminal-server` records that in
`loaf.json`. Readers learn the types from `loaf.json` before any payload.

| Type | Contents | Restore |
| --- | --- | --- |
| `source` | Git history and working files of a source checkout, without settings, installer caches or the virtualenv. | Into the project directory, replacing archived files and keeping others. |
| `config` | Settings, saved Modelfiles and APT source files, without the terminal key. Links stay links and must point to another archived configuration file. | Into the configuration directory by single-line edits, keeping the backup's namespace and, in the system deployment, its terminal key path. |
| `installers` | Python wheels, NVIDIA and Docker Debian packages, and the standalone Python runtime downloads of a source checkout's executable builds, the same for every channel. | Into `install-cache/`. |
| `images` | The three core Docker images and the matching shared Open Terminal image when available locally. | `docker image load`. |
| `models` | The Ollama manifests and saved Modelfiles, streamed before the blobs they name. | Unpacked inside the volume, new blobs renamed into place, the rest merged by `rsync`; `--delete` mirrors. |
| `webui` | The Open WebUI volume. | Into a new or empty volume. |
| `terminal` | The user volume with each account's `/home/user` folder and the terminal bearer key. Chat containers stay behind. | A new or empty user volume with its ownership labels, and the key. Implies `webui`. |

The aliases are `project` for `config` and `installers`, and `volumes` for
`models`, `webui` and, with the terminal server enabled, `terminal`. Without
type names, backup saves every available type: `source` only from a checkout
and `terminal` only with the terminal server enabled and `[terminal_server]
include_in_backup = true`, the default, because Open WebUI's saved connection
needs the key. `--no-terminal-server` leaves out the terminal image and type;
restoring such a backup disables the terminal server in the restored settings
and removes loaf's saved terminal connection from the restored WebUI data
with the network-less WebUI image. Restore recovers everything in the archive. Backup stops all project services before
capturing `webui` or `terminal`, and only the downloader for `models`. Model
snapshots tolerate the selected deployment's serving backend. All Docker
operations use disposable, network-disabled containers from the local
downloader image; neither direction copies the model store first.
[Model transfer](../transfer.md#transfer-models-between-existing-deployments)

Restore reads `loaf.json`, refuses formats outside `[transfer]
backup_version_min` to the release's format and named types missing from the
archive; an alias selects the types of its group that the archive contains.
It checks the Docker preconditions that `loaf.json` determines before it reads
the archive, then every member against `loaf.json` before any change. A
launcher with another hash than the recorded one only gets a warning. It validates volume paths, links, image tags and model manifests
while reading each member once, refuses target volumes that are in use, hold
data or carry other ownership labels, and a different existing terminal key,
and applies types in the order of the table. `backup --verify` runs the same
member checks on the stream, without Docker or temporary copies. Links stay
inside their type: `config` links point to archived configuration files and
`source` links stay in the checkout; backup refuses others. Only `source`,
`installers` and `models` restore into another namespace. Restore starts services only when
Modelfile replay is enabled. After restoring `models`, `--import` starts
offline Ollama alone and applies saved Modelfiles. Add `--cpu` to run the
replay on CPU. The optional backup `--export` step refreshes selected cached
Modelfiles before capturing `models`. Both options run under the transfer's
project lock. A prepared compatible host can restore and start offline
without network access. [Backup and restore](../transfer.md)

Restored settings and installers are owner-only. In the system deployment
they are readable by the group of `/etc/loaf` and `/var/lib/loaf`, the
`docker` group after installation, and new directories are 0750 with that
group. The terminal key never takes its mode from a directory: it is
owner-only in a project and readable by root and the `docker` group in the
system deployment, like the key that the package creates. Compose, run by
that group, reads it. A settings file is never restored at the key's path.
[Restore behavior](../transfer.md#restore-behavior)

Setup and restore run in a backup folder make it the project. They move the
archive and its launcher unchanged to
`backup/<archive name>/`, where later restores find it, and keep a copy of
the launcher at the root. Restore creates the default settings in a project
without them. Both end with the command to run next. Other
commands, and a missing command, refuse a backup folder as the project and
print the typical next steps with a pointer to `loaf docs`. With the loaf
Debian package installed, setup and restore still use the folder and also
print the `--system` restore command; loaf never selects the system
deployment by itself. To keep the backup on its media, run its launcher from
the project directory. Only `setup terminal-server-key` creates a bearer
key, from the backup when run in its folder; restore brings the key of the
`terminal` type. Standalone setup recovers `installers` for its
`docker-deps` and `toolkit` parts and copies the launcher into the project.
Restoring `source` replaces existing files with the archived ones and puts
them back if publishing fails; guides that standalone setup exported give way
to the checkout's `docs/` as a whole. Standalone setup exports guides only
outside a source checkout.
[Backup recovery](../transfer.md#debian-restore)

Source setup caches Python wheels in `install-cache/wheels/` and matching
NVIDIA Toolkit installers in `install-cache/deb/nvidia/`. On Debian 12 or 13,
the `docker-deps` part prepares Engine, CLI, Compose, Buildx and their
required packages with a local APT index under `install-cache/deb/docker/`.
Debian 13 uses its own Docker packages. Debian 12 uses Docker's repository,
trusted through a pinned key, with a private APT state. Each Docker cache targets one Debian release and
package architecture. Standalone setup can install matching packages from
the backup with the same parts. [Debian installation](../installation.md#disconnected-debian-installation)
