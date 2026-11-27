# loaf command reference

[CLI overview](cli.md) · [Project overview](../README.md)

```text
loaf [GLOBAL OPTIONS] COMMAND [OPTIONS]
```

`loaf --help` lists commands and global flags. `loaf COMMAND --help` shows the
flags for that command. Flags can appear before or after the command. Relative
paths given on the command line start at the shell's current directory.

## Global flags

| Flag | Effect |
| --- | --- |
| `-h`, `--help` | Print help for `loaf`, a command, or an integration and exit. |
| `--project-dir PATH` | Select the checkout or deployment. Defaults to the current directory. Setup and restore make a backup folder the project; other commands refuse one. [Debian restore](transfer.md#debian-restore) |
| `--system` | Operate the system deployment with settings in `/etc/loaf` and data in `/var/lib/loaf`. The Debian package uses it unless `--project-dir` or `--config-dir` selects a project. [Debian package](installation.md#debian-package) |
| `-v`, `--verbose` | Show external commands where available. With `test`, show individual test names. |

The standalone executable also accepts `./loaf --license` to print its
bundled license notices and source locations. [Standalone license](releases.md#standalone-license)

## Shared command flags

Each command lists the shared flags it uses.

| Flag | Effect |
| --- | --- |
| `--config-dir PATH` | Override the project's `config/` directory. Packaged defaults supply unset values. |
| `--version VERSION` | Select a separate deployment namespace. Use `--version=` to clear the configured value. [Versioned deployments](launcher.md#run-a-versioned-deployment) |
| `--dry-run` | Preview the operation without invoking Docker or taking a project lock. Add `-v` to see planned Compose commands. |
| `-a`, `--attach` | Follow new Compose logs after a successful operation. Press Ctrl+C to leave the log view with services still running. |
| `--wait-timeout SECONDS` | Override the readiness wait for service startup. The packaged default is 300 seconds. |
| `--models-file PATH` | Select a model INI for this run. It overrides `[launcher] models_file`. [Model selection](launcher.md#select-models) |
| `--backup-dir PATH` | Override `[transfer] backup_dir`. The packaged default is `backup/` relative to the project. Use either `--backup-dir` or `--archive`. |
| `--archive PATH` | Select an explicit archive or backup folder, as specified by the command. |

## Commands

### setup

Prepare configuration and link the Docker project with the standalone
executable, or install `loaf` in the checkout's virtualenv and prepare the
installer cache from source. Setup also creates the configured terminal
server key when it is missing. [Installation](installation.md)

Uses `--config-dir PATH` and `--dry-run`.

- `--with-docker-deps` caches Docker Engine, Compose and Buildx packages on Debian 12 or 13 and installs them as needed. [Docker packages](debian.md#install-docker-and-cache-its-packages)
- `--archive PATH` selects a backup folder or archive when running standalone setup. Inside a backup folder, setup makes the folder the project, uses the backup without this flag and prints the restore command to run next. With `--with-docker-deps`, setup recovers the backup's `installers` first. Setup reuses the backup's terminal key from `chats` and copies the launcher into the project. [Debian restore](transfer.md#debian-restore)
- `--terminal-server-key` creates the configured key only, with owner-only permissions. It preserves an existing key and reuses a key from the selected backup.
- `--test-local-repository` downloads three command-line tools, chosen at random from a fixed list, with the dependencies that the terminal image lacks into `apt-test-repository/`. It writes `config/apt-test-repository.list` and prints the tools and the settings that use them for chat packages. It needs the built terminal image and network access. [Chat packages](terminal-server.md#install-packages-for-a-chat)

In the system deployment, setup creates the terminal server key, readable by
root and the `docker` group. The Debian package's dependencies replace
`--with-docker-deps`. `tests`, `--test-local-repository` and `--archive` need a project.

`loaf setup tests` caches the pinned test packages as wheels and installs
them into the virtualenv that runs `loaf`. It needs Python 3.11 or newer and
a source checkout, and accepts only `--dry-run` and the global flags.
[Test packages](launcher.md#test-packages)

### install

Alias for [`setup`](#setup) with the same flags.

### package

Build release files from the working source checkout into `dist/`. Name one
or both targets; they run in the given order and stop at the first failure.
Uses `--dry-run`. [Local builds](releases.md#build-and-check-locally)

- `binary` builds or replaces the standalone executable `dist/loaf` and its `dist/LICENSE.txt`.
- `debian` builds `dist/loaf_<version>_all.deb` from the launcher wheel in `install-cache/wheels/`. It needs `dpkg-deb`.

### clean

Remove the runtime cache and recognized generated temporary files. Uses
`--dry-run`. [Cleanup and preserved files](launcher.md#clean-generated-files)

- `--full` also removes generated installation files, including the install cache and project virtualenv, when eligible for cleanup.

### version

Print the loaf release, how it was installed, the Python interpreter, the
deployment data directory and the default deployment. Takes the global flags.

### docs

List the guides bundled with this release, or print one with
`loaf docs NAME`. `loaf docs loaf.1` prints the manual page, for example
`loaf docs loaf.1 | man -l -`. Standalone setup also writes the guides to
`docs/` in the deployment. [Bundled guides](launcher.md#read-the-bundled-guides)

### link-docker-project

Create the `docker-project` symlink to the packaged Compose project. Uses
`--dry-run`. [Direct Compose use](launcher.md#link-the-docker-project)

### build

Build the local Ollama and WebUI images and the terminal image when enabled.
Uses `--config-dir PATH`, `--version VERSION`, `--dry-run`, and `-a` or
`--attach`.

### upgrade

Pull updated upstream base images and rebuild the same local images as
`build`. Uses `--config-dir PATH`, `--version VERSION`, `--dry-run`, and `-a`
or `--attach`.

### download

Start the downloader and pull the selected models into the shared Ollama
store. Uses `--models-file PATH`, `--config-dir PATH`, `--version VERSION`,
`--dry-run`, `--wait-timeout SECONDS`, and `-a` or `--attach`.

### online

Start Ollama and WebUI on the connected profile. Uses `--config-dir PATH`,
`--version VERSION`, `--dry-run`, `--wait-timeout SECONDS`, and `-a` or
`--attach`.

- `--cpu` selects CPU inference. The default uses NVIDIA GPU acceleration.

### offline

Start Ollama and WebUI with the offline profile and the saved model store.
Uses `--config-dir PATH`, `--version VERSION`, `--dry-run`,
`--wait-timeout SECONDS`, and `-a` or `--attach`.

- `--cpu` selects CPU inference. The default uses NVIDIA GPU acceleration.

### status

Check serving readiness and list containers across the selected deployment's
profiles. Uses `--config-dir PATH`, `--version VERSION`, `--dry-run`, and `-a`
or `--attach`. [Readiness checks](launcher.md#check-readiness-with-status)

- `--cpu` checks CPU serving readiness and skips the NVIDIA probe.

### stop

Stop the selected deployment's services and remove their containers. Uses
`--config-dir PATH`, `--version VERSION`, `--dry-run`, and `-a` or `--attach`.
[Container removal](launcher.md#stop-and-remove-containers)

- `--remove-containers` explicitly enables container removal, the default.
- `-n`, `--no-remove-containers` keep the stopped containers and their logs.

### backup

Create a dated backup folder containing the archive, its checksums and the
standalone launcher. Name the types `source`, `config`, `installers`,
`images`, `models`, `webui` and `chats`, or the aliases `project` and
`volumes`, or omit them to save every available type. `source` is available
in a source checkout; `chats` adds `webui`. Uses `--config-dir PATH`,
`--version VERSION`, `--dry-run`, and `--backup-dir PATH`. Use `-a` or
`--attach` when creating a backup. [Archive types](transfer.md#select-archive-types) ·
[Backup contents](transfer.md#archive-contents)

- `--verify` checks the whole archive, lists its types and reports any image platforms without Docker. `--archive PATH` selects the backup to verify.
- `--no-terminal-server` skips the Open Terminal image. Backup includes the matching local image by default when it exists. `chats` follows `[terminal_server] include_in_backup` when no types are named.
- `--export`, `--no-export` enable or disable refreshing selected Modelfiles from cached models before saving `models`. The default comes from `[transfer] export_models`.
- `--models-file PATH`, `--wait-timeout SECONDS` apply when refreshing Modelfiles.

### restore

Restore types from a verified backup. Name them as for `backup`, or omit them
to restore everything in the archive. `models` merge into the Ollama store.
`webui` and `chats` require new or empty volumes; `chats` adds `webui`.
`source` replaces existing files with the archived ones and keeps the
others. Run in a backup folder, restore makes it the project and moves the
backup to `backup/`. Restore ends with the command to run next. Uses `--config-dir PATH`,
`--version VERSION`, `--dry-run`, and `-a` or `--attach`. `--archive PATH`
selects a specific backup folder or archive. `--backup-dir PATH` selects the
newest backup for the deployment in that directory; without one, restore
names a backup of another deployment version to select with `--archive`.
A selected backup supplies its saved deployment version. Only `source`, `installers` and
`models` restore into another `--version`. [Restore behavior](transfer.md#restore-behavior)

- `--delete` mirrors the archived models by removing destination-only blobs, manifests and Modelfiles.
- `--import`, `--no-import` enable or disable starting offline Ollama to apply saved Modelfiles after restoring `models`. The default comes from `[transfer] import_models`.
- `--cpu` uses CPU inference when starting Ollama to apply Modelfiles.
- `--wait-timeout SECONDS` applies when starting Ollama to import Modelfiles.

### integration

Choose an integration to print. `loaf integration --help` lists the names.

#### integration systemd

Print a systemd unit for offline serving. Uses `--config-dir PATH` and
`--version VERSION`. [Systemd setup](installation.md#systemd)

#### integration continue

Print Continue YAML for the selected models. Uses `--config-dir PATH` and
`--models-file PATH`. [Continue setup](installation.md#continue-for-vs-code)

#### integration tether

Print Open WebUI Tether installation instructions. Takes the global flags.
[Tether setup](tether.md)

### test

Run the checkout's unittest suite. Uses `--config-dir PATH` to select the
test settings. Use `-v` for individual case names. [Verification guide](verification.md)

- `--badges` records the run and refreshes badges for the groups that ran.
- `--cpu` enables Docker checks and runs the model workflows with CPU inference.
