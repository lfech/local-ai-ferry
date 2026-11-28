# loaf command reference

[Project overview](../README.md)

This page is the one place that explains each command and option. The other
guides describe workflows and link here.

```text
loaf [GLOBAL OPTIONS] COMMAND [OPTIONS]
```

Run `loaf` from the project directory, or select another one with
`--project-dir PATH`. `loaf --help` lists commands and global options, and
`loaf COMMAND --help` the options of a command. Options can appear before or
after the command. Relative paths start at the shell's current directory.
Write option names in full; `loaf` does not accept abbreviations.

```bash
loaf --help
loaf run offline --help
loaf integration systemd --help
```

## Global options

| Option | Effect |
| --- | --- |
| `-h`, `--help` | Print help for `loaf`, a command, an integration or a `docker` command and exit. |
| `--project-dir PATH` | Select the checkout or deployment. Defaults to the current directory. Setup and restore make a backup folder the project; other commands refuse one and print the typical next steps. [Debian restore](transfer.md#debian-restore) |
| `--system` | Operate the system deployment with settings in `/etc/loaf` and data in `/var/lib/loaf`. The Debian package uses it unless `--project-dir` or `--config-dir` selects a project. [Debian package](installation.md#debian-package) |
| `-v`, `--verbose` | Show external commands where available. With `test`, show individual test names. |

The standalone executable also accepts `./loaf --license` to print its
bundled license notices and source locations.

[Standalone license](releases.md#standalone-license)

## Shared options

Each command lists the shared options it uses.

| Option | Effect |
| --- | --- |
| `--config-dir PATH` | Use PATH for the project's settings instead of `config/`. Packaged defaults supply unset values. [Settings layers](configuration.md#project-configuration) |
| `--namespace NAME` | Select the separate deployment namespace `loaf-NAME`. Use `--namespace=` for the default namespace `loaf` instead of the configured one. [Separate namespaces](launcher.md#run-a-separate-namespace) |
| `--dry-run` | Preview the operation without invoking Docker or taking a project lock. It refuses what a real run would refuse. Add `-v` to see the planned Compose commands. |
| `-a`, `--attach` | Follow new Compose logs after a successful operation. Commands that start services follow those services; others follow the selected deployment. Press Ctrl+C to leave the log view with services still running. |
| `--wait-timeout SECONDS` | Override `[launcher] wait_timeout`, the readiness wait for service startup. `0` waits without a limit; negative values are refused. The packaged default is 300 seconds. On a terminal, Compose shows each service's elapsed time; other output gets a line every 10 seconds. |
| `--models-file PATH` | Select a model INI for this run. It overrides `[launcher] models_file`. [Model selection](launcher.md#select-models) |
| `--backup-dir PATH` | Override `[transfer] backup_dir`. The packaged default is `backup/` relative to the project. Use either `--backup-dir` or `--archive`. |
| `--archive PATH` | Select an explicit archive or backup folder, as specified by the command. |

During a run, `loaf` reports the major steps and keeps the progress of Docker
and Ollama visible. Transfers report bytes and throughput.

## Commands

### setup

Prepare a deployment in parts. Name one or more parts, or omit them for
`all`, the default parts of the installation. `loaf setup all PART` adds a
part to them. Parts run in the order of the list below.

| Part | Effect | In `all` |
| --- | --- | --- |
| `docker-deps` | Caches Docker Engine, Compose and Buildx packages on Debian 12 or 13 and installs them as needed. [Docker packages](debian.md#install-docker-and-cache-its-packages) | No |
| `venv` | Source checkout: installs `loaf` into the checkout's `.venv`, caches the Python wheels and links `./loaf`. It then prints the command that activates the virtualenv. [Debian host setup](debian.md#install-the-loaf-command) | Checkout |
| `toolkit` | Caches the NVIDIA Container Toolkit packages in `install-cache/` and installs them as needed. | Checkout |
| `config` | Creates the project's settings: a `config.ini` with every key commented out and a copy of the model list in effect. [Settings files](configuration.md#project-configuration) | Yes |
| `docs` | Outside a source checkout: writes the guides of the release to `docs/`. A source checkout carries its own guides. | Outside a checkout |
| `terminal-server-key` | Creates the configured terminal server key with owner-only permissions. Run in or with a backup folder, it takes the backup's key, which the backup's Open WebUI data uses. [Terminal server key](terminal-server.md#enable-and-connect) | No |
| `create-file-repository` | Creates a local APT file repository for testing `apt` in chat containers. It downloads a fixed set of command-line tools with the dependencies that the terminal image lacks into `apt-test-repository/`, writes `config/apt-test-repository.list` and prints the settings that use them. It needs the built terminal image and network access. [Install packages in a chat](terminal-server.md#install-packages-in-a-chat) | No |

A part keeps what already exists: settings files, guides, a key or a test
repository. It says so and names `--force`, which replaces them. Guides of
the same release need no replacement. The virtualenv and the installer caches
are updated in place.

Every installation uses its own deployment files: the Compose file, the build
contexts and the packaged defaults that `loaf version` locates. A project holds
only settings and data. A source checkout is a project; elsewhere, the settings
that `setup config` creates make one.

- Run in a backup folder, setup makes the folder the project, moves the
  backup to `backup/`, copies the backup's launcher into the project and
  prints the restore command to run next. There, `docker-deps` and `toolkit`
  install from the backup's `installers`.
- When the terminal server is enabled but its key is missing, setup prints
  the command that creates it.
- `--archive PATH` selects the backup folder or archive for setup outside the
  backup folder.
- `-f`, `--force` replaces what a part would keep.
- `--allow-root` lets source setup run as root in a checkout that another user
  owns. Without it, setup refuses, because root would own the virtualenv and
  caches. Run setup as the owner instead; the APT steps use `sudo`.

Uses `--config-dir PATH` and `--dry-run`. In the system deployment, the Debian
package prepares the settings, directories and key: setup accepts `all`,
which only reports this, and `terminal-server-key`, which makes the key
readable by root and the `docker` group. `--archive` needs a project. The
former options `--with-docker-deps`, `--terminal-server-key` and
`--create-file-repository` name the part that replaced them;
`link-docker-project` and `--link-docker-project` explain that loaf needs no
`docker-project` link.

`loaf setup tests` caches the pinned test packages as wheels and installs
them into the virtualenv that runs `loaf`. It needs Python 3.11 or newer and
a source checkout, and accepts only `--dry-run` and the global options.

[Inspect the containers](launcher.md#inspect-the-containers) ·
[Debian restore](transfer.md#debian-restore) · [Test packages](installation.md#test-packages)

### install

Alias for [`setup`](#setup) with the same options.

### package

Build release files from the working source checkout into `dist/`. Name one
or both targets; they run in the given order and stop at the first failure.
Both run `scripts/build-release.sh` and need Linux on amd64. Uses `--dry-run`.

- `binary` builds or replaces the standalone executable `dist/loaf` and its `dist/LICENSE.txt`.
- `debian` builds `dist/loaf_<version>_all.deb` with the standalone executable that system backups carry. It needs `dpkg-deb`.

[Local builds](releases.md#build-and-check-locally)

### clean

Remove loaf's runtime cache under `~/.local/share/loaf/`, or
`$XDG_DATA_HOME/loaf/`, and recognized temporary files and Python bytecode in
the selected project. Uses `--dry-run`.

- `--full` also removes the project virtualenv, generated build and release
  files and the `docker-project` link of an earlier release. It asks before it removes
  `install-cache/`, whose installers an offline host cannot download again,
  and keeps it without a terminal.
- `-f`, `--force` with `--full` removes `install-cache/` without asking. The former `--yes` names it.

Both modes keep Git history and working files, saved backups, settings,
credentials and Docker data. A generated directory that holds Git work, a
backup archive or a mount stays, with a note; the checked Python downloads in
`install-cache/python/` are no backups. Lock files stay so that commands can
coordinate. Cleanup resolves the configured terminal key path first, so an
invalid configuration stops it. After a full clean, run setup again with the
original standalone executable, or the source setup command in a checkout.

### version

Print the loaf release, how it was installed, the Python interpreter, the
deployment data directory and the default deployment. Takes the global options.

### docs

List the guides bundled with this release, or print one with
`loaf docs NAME`. `loaf docs loaf.1` prints the manual page, for example
`loaf docs loaf.1 | man -l -`. Standalone setup writes the guides to `docs/`
in the deployment. Their links to deployment files and other repository files
open the release in the repository; `loaf version` names the local copy of the
deployment files. The Debian package also installs the manual page for
`man loaf`.

### completion

Manage Bash completion for `loaf`. It completes commands, subcommands,
archive and resource types, guide names and options. Option values complete
as directories for `--project-dir`, `--config-dir` and `--backup-dir`, and as
files for `--models-file` and `--archive`. Takes the global options.

[Bash completion setup](launcher.md#bash-completion)

#### completion install

Write the completion script for the current user to bash-completion's
per-user directory, normally `~/.local/share/bash-completion/completions/loaf`.
`XDG_DATA_HOME` and `BASH_COMPLETION_USER_DIR` move it as they do for
bash-completion. It replaces an earlier loaf script and refuses a file that
loaf did not write. With the Debian package, it reports the system script and
writes nothing.

#### completion remove

Remove the per-user script that `install` wrote. The Debian package's system
script stays until the package is removed.

#### completion print

Print the completion script to standard output.

### build

Build the local Ollama and WebUI images and the terminal image when enabled.
Uses `--config-dir PATH`, `--namespace NAME`, `--dry-run`, and `-a` or
`--attach`.

[Image recipes](configuration.md#select-an-ollama-build-recipe)

### upgrade

Pull updated upstream base images and rebuild the same local images as
`build`. Restart the serving profile to use them. Uses `--config-dir PATH`,
`--namespace NAME`, `--dry-run`, and `-a` or `--attach`.

### download

Stop the serving services, start the downloader and pull the selected models
into the shared Ollama store. A model with a `name` gets that name, and its
long `hf.co/…` name is removed. Prints the command that starts offline serving
next. Uses `--models-file PATH`, `--config-dir PATH`, `--namespace NAME`,
`--dry-run`, `--wait-timeout SECONDS`, and `-a` or `--attach`.

[Model selection](launcher.md#select-models)

### run

`loaf run offline` or `loaf run online` starts Ollama and WebUI with the
saved model store, and the terminal server when enabled. Starting one profile
stops the other, because both use the same volumes.

- `offline` puts Ollama on an internal network and gives WebUI its offline
  settings. With `[terminal_server] network = connected`, it notes that chat
  containers that models drive can reach every address the Docker host reaches.
- `online` gives Ollama and WebUI internet access.

Uses `--config-dir PATH`, `--namespace NAME`, `--dry-run`,
`--wait-timeout SECONDS`, and `-a` or `--attach`. The former commands
`loaf offline` and `loaf online` point to this one.

- `--cpu` selects CPU inference. The default uses NVIDIA GPU acceleration.

[Offline operation](configuration.md#networking-and-offline-operation) ·
[CPU serving](launcher.md#serve-on-cpu)

### restart

`loaf restart offline` or `loaf restart online` stops that profile and
removes its containers like `loaf stop PROFILE`, then starts it like
`loaf run PROFILE`, under one project lock. Every service of the profile
starts with a new container, while `run` replaces only containers whose
settings changed. The terminal server restarts too, unless the other profile
serves. Without a profile, restart restarts the profile that serves now and
refuses when none or both have running containers; a dry run then cannot ask
Docker and needs the profile.

Uses `--config-dir PATH`, `--namespace NAME`, `--dry-run`,
`--wait-timeout SECONDS`, and `-a` or `--attach`.

- `--cpu` starts the profile with CPU inference.

[CPU serving](launcher.md#serve-on-cpu)

### status

Check serving readiness and list the containers across the selected
deployment's profiles. The report opens with the deployment name, project
directory, WebUI URL and settings files, then checks:

- Docker and Compose: Compose 2.26.1 through 2.29.x warn and use the NVIDIA
  device reservation; older versions fail. Docker Engine 26 and 27 warn about
  Open Terminal's legacy bridge isolation.
- Configuration: the launcher settings, the resolved Compose configuration
  and, with the terminal server enabled, its key and APT source settings.
- Local images and NVIDIA GPU access from a disposable container.
- Storage and networking: volume readability, local model data including
  Ollama's `manifests-v2` catalog, and the WebUI host ports. A missing model
  volume names `loaf download` and `loaf restore models`; a missing WebUI
  volume is created on first startup. Host port checks need a local Docker
  endpoint.

A failure gives a nonzero exit status. Uses `--config-dir PATH`,
`--namespace NAME`, `--dry-run`, and `-a` or `--attach`.

- `--cpu` checks CPU serving readiness and skips the NVIDIA probe.

### stop

Stop the selected deployment's services and remove their Compose containers.
`loaf stop offline` or `loaf stop online` stops only that profile's Ollama and
WebUI; the terminal server stops with them unless the other profile still
serves. Without a profile, `stop` stops every service, the downloader and the
terminal server included. Named volumes, bind-mounted files, images and
networks stay; container logs and files only in a container's writable layer
go. Uses `--config-dir PATH`, `--namespace NAME`, `--dry-run`, and `-a` or
`--attach`. A dry run cannot ask Docker and says how a real run decides.

- `--no-remove-containers` keeps the stopped containers and their logs.

[Compose container removal](https://docs.docker.com/reference/cli/docker/compose/rm/)

### docker

List or remove the selected deployment's Docker resources. Every name starts
with the deployment namespace, `loaf` or `loaf-NAME`. Both commands use
`--config-dir PATH` and `--namespace NAME`.

| Type | Names in the default deployment |
| --- | --- |
| Containers | Compose services such as `loaf-open-webui-1`, and the terminal server's chat containers `loaf-terminal-KEY`. |
| Images | `loaf/ollama-online`, `loaf/ollama-offline`, `loaf/open-webui` and `loaf/open-terminal`, tagged `latest` or the namespace name. |
| Volumes | `loaf-ollama` holds the models, `loaf-open-webui` the Open WebUI accounts and chats, and `loaf-terminal-users` the terminal user folders. |
| Networks | Compose networks such as `loaf_inference`, and the terminal server's chat networks `loaf-terminal-KEY`. |

#### docker ps

List the containers, images, volumes and networks with their state, size,
content or purpose.

#### docker rm

Remove the resources of one or more types: `all`, `containers`, `images`,
`volumes` or `networks`. `rm` lists them and asks for confirmation, stops the
running services within their grace periods, and then removes containers,
networks, volumes and images in that order. Networks, volumes and images can
go only once the deployment has no containers; otherwise `rm` refuses and
prints the command that includes `containers`. A removed image tag that
another deployment also uses keeps the image for that deployment. Base
images, build cache and other projects' resources stay. Uses `--dry-run`.

- `-f`, `--force` removes without asking. Without a terminal, `rm` requires it. The former `--yes` names it.

Removed volumes cannot be recovered; save them first with `loaf backup volumes`.
An offline host cannot rebuild removed images; `loaf restore images` brings
them back.

### backup

Create a dated backup folder with the archive and the standalone `loaf`
executable that can restore it. The archive's first member, `loaf.json`,
records the hash of every member and of the executable. At the end, backup
prints the SHA-256 of the archive and the executable to record apart from the
transfer media.

Name the types `source`, `config`, `installers`, `images`, `models`, `webui`
and `terminal`, or the aliases `project` and `volumes`, or omit them to save
every available type. `source` is available in a source checkout; `terminal`
adds `webui`. Without type names, `terminal` is included when the terminal
server is enabled and `[terminal_server] include_in_backup` is true, the
default.

A source checkout builds the executable from its working files first. Other
installations include the running standalone executable, or the one that the
release wheel and the Debian package carry, or else `loaf` or `dist/loaf` in
the project; the dry run checks this too. Backing up `webui` or `terminal`, or
refreshing Modelfiles, stops the deployment's services, and backup then prints
the command that resumes serving. An `installers` type without NVIDIA toolkit
packages only gets a warning that names the command that caches them.

Uses `--config-dir PATH`, `--namespace NAME`, `--dry-run`, `--backup-dir PATH`,
and `-a` or `--attach`.

- `--verify` runs restore's checks on every member against the hashes in
  `loaf.json`, without Docker or temporary copies, lists the types and reports
  the image platforms. A launcher with another hash than `loaf.json` records
  only gets a warning. It ends with the SHA-256 of the archive and the
  launcher to compare with the recorded values. `--archive PATH` selects the
  backup to verify.
- `--no-terminal-server` leaves out the terminal server: the Open Terminal
  image, the `terminal` type with its user volume and bearer key. Restoring
  such a backup disables the terminal server in the restored settings and
  removes loaf's saved terminal connection from the restored WebUI data.
  Without the option, backup includes the matching local terminal image when
  it exists.
- `--export`, `--no-export` enable or disable refreshing the selected
  Modelfiles from the cached models before saving `models`. The default comes
  from `[transfer] export_models`.
- `--models-file PATH`, `--wait-timeout SECONDS` apply when refreshing Modelfiles.

[Archive contents](transfer.md#archive-contents) ·
[Check a copied backup](transfer.md#check-a-copied-backup) ·
[Archive types](transfer.md#select-archive-types)

### restore

Check a backup and restore its types. All members are checked against the
hashes in `loaf.json` before the first change, and Docker preconditions, such
as empty target volumes, before the archive is read. Name the types as for
`backup`, or omit them to restore everything in the archive. A named type
must be in the archive; an alias selects the types of its group that the
archive contains.

- `models` merge into the Ollama store; `--delete` mirrors them.
- `webui` and `terminal` need new or empty volumes; `terminal` adds `webui`.
- `source` replaces existing files with the archived ones and keeps the others.

Run in a backup folder, restore makes it the project and moves the backup to
`backup/`. A project without settings gets the default `config.ini` and model
list, as from `setup config`. Restoring `source` over guides that standalone
setup exported replaces them with the checkout's `docs/` as a whole. Restore
ends with the command to run next: `setup terminal-server-key`
when the terminal server is enabled but its key is missing, else `run offline`,
with `--cpu` after a CPU replay. A selected backup supplies its saved
namespace. Only `source`, `installers` and `models` restore into another
`--namespace`.

Uses `--config-dir PATH`, `--namespace NAME`, `--dry-run`, and `-a` or
`--attach`. `--archive PATH` selects a backup folder or archive.
`--backup-dir PATH` selects the newest backup for the namespace in that
directory; without one, restore names a backup of another namespace to select
with `--archive`.

- `--delete` mirrors the archived models by removing destination-only blobs, manifests and Modelfiles.
- `--import`, `--no-import` enable or disable starting offline Ollama to apply saved Modelfiles after restoring `models`. The default comes from `[transfer] import_models`.
- `--cpu` uses CPU inference when starting Ollama to apply Modelfiles.
- `--wait-timeout SECONDS` applies when starting Ollama to import Modelfiles.

[Restore behavior](transfer.md#restore-behavior)

### integration

Choose an integration to print. `loaf integration --help` lists the names.

#### integration systemd

Print a systemd unit that starts offline serving and stops the deployment.
Uses `--config-dir PATH` and `--namespace NAME`.

- `--cpu` makes the unit serve with CPU inference.

[Systemd setup](integrations.md#systemd)

#### integration continue

Print Continue YAML for the selected models. Uses `--config-dir PATH` and
`--models-file PATH`.

[Continue setup](integrations.md#continue-for-vs-code)

#### integration tether

Print Open WebUI Tether installation instructions. Takes the global options.

[Tether setup](integrations.md#open-webui-tether)

### test

Run the checkout's unittest suite. Uses `--config-dir PATH` to select the
test settings. Use `-v` for individual case names.

- `--badges` records the run and refreshes badges for the groups that ran.
- `--cpu` enables Docker checks and runs the model workflows with CPU inference.

[Verification guide](verification.md)
