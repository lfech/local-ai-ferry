# Backup and transfer

[Project overview](../README.md)

Run `loaf backup` on the source and copy the entire dated backup folder to the
destination. The folder holds the archive and the standalone Linux amd64
launcher that can restore it. Run `./loaf setup` and
`./loaf restore` inside the folder. The folder becomes the project, and the
backup moves unchanged to its `backup/` directory. To keep the backup on
read-only media, run its `loaf` from the project directory instead.
[Debian restore](#debian-restore)

## Select archive types

Name the types to save or restore. Without names, backup saves every
available type and restore recovers everything in the archive.

| Type | Contents | Restored to |
| --- | --- | --- |
| `source` | Git history and working files of a source checkout, without settings, installer caches or the virtualenv | The project directory, replacing existing files with the archived ones |
| `config` | Settings, saved Modelfiles and APT source files from `config/`, with links to other files there | The configuration directory |
| `installers` | Cached Python wheels, NVIDIA and Docker Debian packages and the standalone Python runtime downloads from `install-cache/` | The project's `install-cache/` |
| `images` | The three core images and the matching Open Terminal image when available locally | The Docker image store |
| `models` | Ollama blobs, manifests and saved Modelfiles | The Ollama volume, merged |
| `webui` | The Open WebUI volume: accounts, chat history, settings and the saved terminal connection | A new or empty volume |
| `terminal` | The user volume with each account's `/home/user` folder, and the terminal bearer key | A new or empty user volume and the configured key file |

| Alias | Types |
| --- | --- |
| `project` | `config`, `installers` |
| `volumes` | `models`, `webui`, and `terminal` when the terminal server is enabled. Restore selects those that the archive contains. |

`source` exists only in a source checkout. Backups without names include
`terminal` when the terminal server is enabled and `[terminal_server]
include_in_backup` is true, the default. Open WebUI saves its terminal
connection with the bearer key in `webui`, so a restored WebUI needs the same
key. The archive then contains the key in plaintext. `terminal` always brings
`webui`, which owns the accounts that the folders belong to. Chat containers,
with their `/home/chat` folders and installed packages, stay on the source
host.

`loaf backup --no-terminal-server` leaves out the terminal server: the Open
Terminal image and the `terminal` type with the user volume and key.
`loaf.json` records it. A restore of such a backup sets `[terminal_server]
enabled = false` in the restored settings and removes loaf's saved terminal
connection from the restored WebUI data, so Open WebUI starts without a
terminal. The removal runs Python in a network-disabled container from the
local `loaf/open-webui` image; restore `images` too where it is missing.

```bash
loaf backup project images
loaf restore config --archive /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
```

Restore verifies every member before it changes anything and reports a
named type that the archive lacks. An alias restores the types of its group
that the archive contains. A backup that includes `webui` or
`terminal` stops the project services. A backup of `models` alone stops only
the downloader. Restoring Docker types without `images` requires the matching
local `loaf/ollama-online` helper image.

## Transfer models between existing deployments

Select the `models` type on the source and destination.

```bash
loaf backup models
loaf restore models --archive /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
```

The model store is portable across deployment namespaces: restore `models`
with another `--namespace` than the backup's. The same holds for `source` and
`installers`. Backup stops the selected deployment's downloader. The online
or offline serving profile can stay running if its Ollama container is the
only running consumer of the volume.

Restore supports both legacy manifests and Ollama's `manifests-v2`
registrations. V2 tags can be relative links to content-addressed manifest
blobs. The archive check verifies those links, the manifest blobs' SHA-256
hashes, and the presence and sizes of referenced model blobs before restore
changes the destination volume.
[Ollama manifest storage](https://github.com/ollama/ollama/blob/v0.40.17/manifest/manifest.go#L1169)

Layer checks use the stored filename and size. Restore reuses existing blobs
with matching names. A same-size damaged layer can pass these checks and
fail inference. Use a clean model store when recovering from suspected
blob damage.

Both directions run in a disposable container from the downloader image
without networking. Backup streams the store with `tar`, reading the manifests
and saved Modelfiles before the immutable blobs they name; it copies nothing
first. Restore unpacks the archive into a scratch directory inside the model
volume and moves new blobs into place by renaming them, so the volume needs
room for the new blobs only once. `rsync` then updates manifests and
Modelfiles; build or upgrade the downloader image while connected to obtain
it. Keep the model catalog stable during transfer by avoiding model pulls,
creation and deletion through the Ollama API.

Restore moves missing blobs first and updates matching manifests and
Modelfiles. It preserves destination-only data by default. Add `--delete`
to remove blobs, manifests and Modelfiles absent from the archive. This can
remove models currently in use. Use `--dry-run` to check which deployment
and archive you selected. To avoid interrupting requests for removed models,
stop inference before mirroring a store. Start the same serving profile
again after the copy.

## Archive contents

Each backup is a folder named `YYYYMMDD-HHMM_local-ai-ferry`. Use
`--archive PATH` to select a specific backup folder or archive file.

```text
backup/YYYYMMDD-HHMM_local-ai-ferry/
  loaf
  YYYYMMDD-HHMM_local-ai-ferry.tar.gz
```

The archive's first member, `loaf.json`, records the size and SHA-256 hash of
every member and of the launcher. Verify and restore check the members against
it; a launcher with another hash only gets a warning, because the running loaf
restores the archive. A backup folder from a release before 0.5.0 also holds
a `SHA256SUMS` file, which loaf moves with the folder and no longer reads; a
release download keeps its own `SHA256SUMS` for the release files.
[Which executable a backup carries](cli-help.md#backup)

In a source checkout, backup builds `dist/loaf` from the current working
files before accessing Docker. The first build needs a connected machine;
later builds reuse the downloads cached in `install-cache/python/`.
[Local binary build](releases.md#build-and-check-locally)

`loaf.json` also lists the archive's types. loaf reads it
before any payload, so it can refuse an unsupported archive or a missing type
without unpacking the rest. Then each type has one plain tar member.
`terminal` has two: `terminal/users.tar` with the user volume, absent before
any account used the terminal, and `terminal/terminal-server.key`. A backup
made with `--no-terminal-server` adds `"skip_terminal_server": true`.

```json
{
  "format": "loaf-archive",
  "version": 3,
  "created": "2026-11-26T18:30:00+01:00",
  "loaf": "0.2.2",
  "deployment": "",
  "launcher": {"size": 22501263, "sha256": "…"},
  "items": [
    {"type": "config", "members": [{"path": "config.tar", "size": 20480, "sha256": "…"}]},
    {"type": "images", "images": ["loaf/ollama-online:latest", "…"],
     "members": [{"path": "images.tar", "size": 7516192768, "sha256": "…"}]}
  ]
}
```

`deployment` is the backup's namespace name, `[launcher] namespace`. The bearer key stays
out of `loaf.json`. Restore accepts archive format 3, which stores the
terminal type as one user volume. `[transfer] backup_version_min` names the
oldest accepted format. Releases before v1.0.0 read only their own format.
Restore an older backup with the launcher in its folder.

Backups are readable only by their owner. In a setgid directory, such as the
system deployment's `/var/lib/loaf/backup`, they are also readable by the
directory's group.

The default directory is `backup/` in the project. Set `[transfer]`
`backup_dir` in `config.ini` for a persistent choice or pass
`--backup-dir PATH` to either command. An explicit relative path resolves
from the shell's current directory. Backup names use local time.
Restore uses the folder it is run from or the folder containing the
executable. It can also select the latest backup from the configured
directory for the requested namespace. Git ignores the default `backup/`
directory and the generated names.

Keep the transfer folder private. The archive contains WebUI data and may
include Git history. The release executable supplies the packaged defaults and
Docker files. Copy required `.env` settings and other external data
separately. The `config` type excludes the configured terminal key file. A
link in `config/` stays a link when it points, relatively, to another
configuration file there; backup refuses any other link and names it. Open
WebUI's saved connection and its credential are part of `webui`. Restore
`terminal` with `webui`, or update the saved connection to the destination's key.
[Terminal server transfer](terminal-server.md#offline-transfer-and-shutdown)

Source checkout backups prepare the launcher wheel, dependency wheels and wheel
cache record for `installers`. Backup reuses a current cache and refreshes it
with package indexes disabled when the package source or Python environment
changes. Run `loaf install` while connected to populate the cache before your
first backup from Debian. Building `dist/loaf` caches its checked standalone
Python downloads in `install-cache/python/`, so later builds and backups from
the checkout work offline; `installers` carries them.
[Python wheel preparation](launcher.md#prepare-python-installation-offline)

On Debian, run `loaf setup` while connected to fill `install-cache/deb/nvidia/`.
Backup includes packages matching the installed NVIDIA toolkit or a complete
prepared set for the host architecture. Without them, backup warns that the
destination needs the toolkit for GPU inference and names the command that
caches its packages for each installation type. On Debian 12 or 13, run
`loaf setup docker-deps` to include Docker's cached `.deb` files and local APT
index under `install-cache/deb/docker/`. Use compatible wheels and Debian
packages on the destination. [Disconnected installation](installation.md#disconnected-debian-installation)

Backup stages each member, records its size and SHA-256 hash, and publishes
the folder only after the archive is complete. More than one backup in a
minute gets numbered suffixes such as `-2`. Archives use gzip level 4 by
default. Set `compression_level` under `[transfer]` in `config/config.ini` to
choose a level from 0 through 9. Allow disk space for the uncompressed members
and the compressed archive during backup. Restore stages the selected
members in the writable project directory, and the model volume needs room
for the new blobs.

Both commands report per-member progress on standard error. Interactive
terminals show a live bar and ETA when the total is known. Docker exports
show bytes transferred, rate, and elapsed time while they stream. Redirected
logs get periodic updates and a final line for each stage. While Docker prepares
an image export or pauses its stream, the meter shows `waiting for Docker`.

## Verify a backup

```bash
loaf backup --verify --archive /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
```

Verification checks every member against `loaf.json` and runs the same
content checks as restore: paths and links, model manifests and their blobs,
volume payloads, image tags and the bearer key. It lists the types and their
sizes and reports the operating system and architecture of any saved images.
For a backup folder, it reports whether the launcher matches the hash that
`loaf.json` records. It performs no Docker operations and needs no temporary disk
space: it reads the archive once as a stream and keeps only small manifest
files in memory. Without `--archive`, it checks the newest backup in the
configured directory. Select the backup's namespace with `--namespace` when
applicable.

### Check a copied backup

`loaf.json` and the hashes it records travel with the archive. They reveal a
damaged or truncated copy, but not a deliberate change: whoever can change the
transfer media can replace the archive, `loaf.json` and the launcher together.
`loaf backup` therefore ends with the SHA-256 of the archive and the launcher,
in the format of `sha256sum`. Record these lines apart from the media, for
example on paper or in a separate message, and compare them on the destination
before the first restore. `backup --verify` prints the same lines.

```bash
cd /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
sha256sum loaf YYYYMMDD-HHMM_local-ai-ferry.tar.gz
```

Check a release executable or package against the release's `SHA256SUMS` in
the same way. [Release downloads](releases.md#run-the-executable)

## Transfers between namespaces

Use the same `--namespace` for backup and restore.

```bash
loaf --namespace v1-2-3 backup
loaf --namespace v1-2-3 restore --backup-dir /media/transfer
```

The folder name is `YYYYMMDD-HHMM_v1-2-3_local-ai-ferry`.
Restore selects the newest backup for the requested namespace when searching
a backup directory. Inside a specific folder, it uses the saved namespace. It
checks the saved image tags before loading images or extracting volumes. Only
`source`, `installers` and `models` restore into another namespace.
[Separate namespaces](launcher.md#run-a-separate-namespace)

## Check saved Modelfiles during backup

A backup with `models` checks each saved Modelfile against Ollama's current
definition for the same model. It uses the offline Ollama image in a
network-disabled container with the model volume mounted read-only. Backup
warns about differing definitions, missing referenced blobs, and
definitions it cannot check. It writes the archive with the captured files.

The normal offline transfer sequence is `loaf backup`, `loaf restore`,
then `loaf run offline`. Offline startup applies saved Modelfiles, so a
reported difference can affect the model that starts on the destination.

## Export cached models during backup

Use `loaf backup --export` to refresh the selected cached Modelfiles before
saving `models`. It uses the same model selection as `loaf download`.
Pass `--models-file PATH` to choose a different INI. All selected models must
already be cached under their selected names, which `loaf download` creates.
Sources that another entry also selects without a name are copied to their
names again first. If export fails,
no new backup is published. Set `export_models = true` under `[transfer]`
to enable the step by default. `--no-export` skips the configured refresh
and still checks the saved definitions.

## Debian backup

```bash
./loaf backup
./loaf run offline
```

Use the account that created the backup when copying it. New backups are
readable and writable by their owner on filesystems that enforce Unix permissions.

## Debian restore

Copy the complete trusted backup folder to the destination, where it
becomes the project, and [check its hashes](#check-a-copied-backup). Use a
login with Docker access. On Debian 12 or 13, use
`./loaf setup all docker-deps toolkit` in place of the first setup command
when installing Docker and the NVIDIA toolkit from the backup's matching
Debian package cache.
[Docker access](debian.md#docker-access) · [Disconnected Debian installation](installation.md#disconnected-debian-installation)

```bash
cp -r /media/transfer/YYYYMMDD-HHMM_local-ai-ferry ~/loaf
cd ~/loaf
./loaf setup
./loaf restore
./loaf run offline
```

The first setup or restore in a backup folder makes it the project. It moves
the archive and the launcher unchanged to
`backup/YYYYMMDD-HHMM_local-ai-ferry/` and keeps a copy of `loaf` in the
folder. The archive's name gives the directory name, even if you renamed the
folder. Restores find the newest backup of the namespace in the settings.
Setup and restore end with the command to run next. After setup, it includes
`--archive` when the backup belongs to another namespace.

Other commands, or `./loaf` without a command, do not run in a backup
folder. They list the typical next steps, the alternative of running the
backup's `loaf` from a new project directory, and `./loaf docs` for the
guides.

When the loaf Debian package is installed, setup and restore still make the
folder the project. They also print the command for the system deployment:
`sudo ./loaf --system restore --archive PATH`, with the backup's path after
the move. [Debian package](installation.md#debian-package)

To leave the backup on its media, create the project directory and run the
backup's `loaf` by its full path from there:

```bash
mkdir -p ~/loaf && cd ~/loaf
/media/transfer/YYYYMMDD-HHMM_local-ai-ferry/loaf setup
/media/transfer/YYYYMMDD-HHMM_local-ai-ferry/loaf restore
./loaf run offline
```

To recover a source checkout, restore `source`, or everything. Restore
writes the archived files, including `python-package/`, `docs/` and the Git
history, over existing files in the project and keeps files that the
archive lacks. Local changes to archived files are lost. A file where the
archive has a directory, or the reverse, stops the restore before any
change. If moving the files into place fails, restore puts back the files
it replaced. Backups leave the host-specific virtualenv behind. Run
`PYTHONPATH=python-package/src python3 -m loaf setup` in the project to
rebuild `.venv` from the restored wheels when you develop there.

```bash
./loaf restore --project-dir ~/src/local-ai-ferry
```

## Restore behavior

Restore first checks the Docker preconditions that `loaf.json` determines,
so that a large archive is not read in vain: the helper image, and that an
existing WebUI or terminal user volume is unused and empty. The emptiness
check runs the local downloader image without network; restore `images` first
where it is missing. Then it checks every member's hash and content, and the
user volume's ownership labels and storage settings against the settings in
effect after the restore. Nothing changes before all checks pass. Restore applies the
selected types in this order: `source`, `config`, `installers`, `images`,
`models`, `webui`, `terminal`. Restoring `config` keeps the backup's
namespace and changes only the lines it needs in `config.ini`, so comments
and other settings stay. A `[launcher] version` line from a backup made
before the rename becomes `[launcher] namespace`. Restoring `models` or `webui` creates a missing volume with the
labels that Compose gives its own volumes, so Compose later uses it without a
warning. Restoring `terminal` creates the user volume with its ownership
labels and writes the bearer key with owner-only permissions. It refuses a different existing key. Restored
settings and installers are owner-only as well.

In the system deployment, restored settings in `/etc/loaf` and installers
in `/var/lib/loaf` are readable by the group of those directories, which is
the `docker` group, and new directories are open to that group. The bearer
key keeps its own rule: like the key from `loaf --system setup`, it is
readable by root and the `docker` group and never by others. Restoring
`config` keeps the key at `/etc/loaf/terminal-server.key`, even when the
backup's settings name another path. Writing `/etc/loaf` needs root, so run
the system restore with `sudo`. In every deployment, restore refuses a
settings file at the key's path.

Restore starts services only when Modelfile replay is enabled. A project
without settings gets the default `config.ini` and model list, as from
`loaf setup config`. Restoring `source` into a project whose `docs/` holds
the guides that standalone setup exported replaces them with the checkout's
guides as a whole. On a recovered host, run the
printed next command, normally `loaf run offline`, and open the printed address
to sign in or create the first account. When the restored settings enable the
terminal server but its key is missing, the next command is
`loaf setup terminal-server-key`. A restored `apt_repository_dir` setting
needs its repository directory, which no archive type carries; copy it or
create it again with `loaf setup create-file-repository`.

Add `--import` or set `import_models = true` under `[transfer]` to start offline
Ollama and import all saved Modelfiles after restoring `models`.
The default uses NVIDIA container access. Add `--cpu` to run the import on CPU.
This step leaves WebUI stopped. Run
`loaf run offline` to start WebUI. If import fails, the restored data remains in
place. Fix the problem and start the desired serving mode. Use `--no-import`
to skip a configured import.

Keep model files in Linux named volumes. Paths can include colons.
