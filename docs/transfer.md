# Backup and transfer

[Project overview](../README.md)

Run `loaf backup` on the source and copy the entire dated backup folder to the
destination. The folder holds the archive, the standalone Linux amd64
launcher that can restore it, and their checksums. Run `./loaf setup` and
`./loaf restore` inside the folder. Both prepare the project
`~/.local-ai-ferry` and leave the folder unchanged, so it can stay on
read-only media. [Debian restore](#debian-restore)

## Select archive types

Name the types to save or restore. Without names, backup saves every
available type and restore recovers everything in the archive.

| Type | Contents | Restored to |
| --- | --- | --- |
| `source` | Git history and working files of a source checkout, without settings, installer caches or the virtualenv | The project directory, which must not contain any of these files yet |
| `config` | Settings, saved Modelfiles and APT source files from `config/` | The configuration directory |
| `installers` | Cached Python wheels, NVIDIA and Docker Debian packages from `install-cache/` | The project's `install-cache/` |
| `images` | The three core images and the matching Open Terminal image when available locally | The Docker image store |
| `models` | Ollama blobs, manifests and saved Modelfiles | The Ollama volume, merged |
| `webui` | The Open WebUI volume: accounts, chat history, settings and the saved terminal connection | A new or empty volume |
| `chats` | Each account's `/home/user`, each saved chat's `/home/chat`, the chats' package images and the terminal bearer key | New or empty volumes and the configured key file |

| Alias | Types |
| --- | --- |
| `project` | `config`, `installers` |
| `volumes` | `models`, `webui`, and `chats` when the terminal server is enabled |

`source` exists only in a source checkout. Backups without names include
`chats` only with `[terminal_server] include_in_backup = true`, because the
archive then contains the bearer key in plaintext. `chats` always brings
`webui`, which owns the accounts and chats that the folders belong to. A
temporary chat's folder and package image stay on the source host.

```bash
loaf backup project images
loaf restore config --archive /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
```

Restore verifies every member before it changes anything and reports any
requested type missing from the archive. A backup that includes `webui` or
`chats` stops the project services. A backup of `models` alone stops only
the downloader. Restoring Docker types without `images` requires the matching
local `local/ollama-online` helper image.

## Transfer models between existing deployments

Select the `models` type on the source and destination.

```bash
loaf backup models
loaf restore models --archive /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
```

The model store is portable across deployment namespaces: restore `models`
with another `--version` than the backup's. The same holds for `source` and
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

Both directions use `rsync` in a disposable container without networking.
Build or upgrade the downloader image while connected to obtain `rsync`.
Keep the model catalog stable during transfer by avoiding model pulls,
creation and deletion through the Ollama API.

Restore copies missing blobs first and updates matching manifests and
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
  SHA256SUMS
```

`SHA256SUMS` lists the launcher and the archive. Check both without loaf
with `sha256sum -c SHA256SUMS` inside the folder. In a source checkout, use a
connected machine. Backup builds `dist/loaf` from the current working files
before accessing Docker and includes the result in the folder. A standalone
deployment includes the binary that launched it.
[Local binary build](releases.md#build-and-check-locally)

The archive's first member, `loaf.json`, lists its types. loaf reads it
before any payload, so it can refuse an unsupported archive or a missing type
without unpacking the rest. Then each type has one plain tar member. `chats`
has three: `chats/volumes.tar`, `chats/images.tar` when chats have package
images, and `chats/terminal-server.key`.

```json
{
  "format": "loaf-archive",
  "version": 2,
  "created": "2026-11-26T18:30:00+01:00",
  "loaf": "0.2.2",
  "deployment": "",
  "items": [
    {"type": "config", "members": [{"path": "config.tar", "size": 20480, "sha256": "…"}]},
    {"type": "images", "images": ["local/ollama-online:latest", "…"],
     "members": [{"path": "images.tar", "size": 7516192768, "sha256": "…"}]}
  ]
}
```

`deployment` is the backup's `[launcher] version`. The `chats` item also
lists the account folders, the saved chats by ID and owner, and the package
images; the bearer key stays out of `loaf.json`. Restore accepts archive
format 2. `[transfer] backup_version_min` names the oldest accepted format.
loaf releases before format 2 wrote another layout. Restore such a backup
with the launcher in its folder.

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
separately. The `config` type excludes the configured terminal key file. Open
WebUI's saved connection and its credential are part of `webui`. Restore
`chats` with `webui`, or update the saved connection to the destination's key.
[Terminal server transfer](terminal-server.md#offline-transfer-and-shutdown)

Source checkout backups prepare the launcher wheel, dependency wheels and wheel
cache record for `installers`. Backup reuses a current cache and refreshes it
with package indexes disabled when the package source or Python environment
changes. Run `loaf install` while connected to populate the cache before your
first backup from Debian.
[Python wheel preparation](launcher.md#prepare-python-installation-offline)

On Debian, run `loaf setup` while connected to fill `install-cache/deb/nvidia/`.
Backup includes packages matching the installed NVIDIA toolkit or a complete
prepared set for the host architecture. On Debian 13, add
`--with-docker-deps` to include Docker's cached `.deb` files and local APT
index under `install-cache/deb/docker/`. Use compatible wheels and Debian
packages on the destination. [Disconnected installation](installation.md#disconnected-debian-installation)

Backup stages each member, records its size and SHA-256 hash, and publishes
the folder only after the archive is complete. More than one backup in a
minute gets numbered suffixes such as `-2`. Archives use gzip level 4 by
default. Set `compression_level` under `[transfer]` in `config/config.ini` to
choose a level from 0 through 9. Allow disk space for the uncompressed members
and the compressed archive during backup. Restore stages the selected
members in the writable project directory.

Both commands report per-member progress on standard error. Interactive
terminals show a live bar and ETA when the total is known. Docker exports
show bytes transferred, rate, and elapsed time while they stream. Redirected
logs get periodic updates and a final line for each stage. While Docker prepares
an image export or pauses its stream, the meter shows `waiting for Docker`.

## Verify a backup

```bash
loaf backup --verify --archive /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
```

Verification checks every member against `loaf.json`, lists the types and
their sizes, and reports the operating system and architecture of any saved
images. For a backup folder, it also checks the launcher and the archive
against `SHA256SUMS`. It performs no Docker operations. Allow temporary disk
space when the archive contains `images`. Without `--archive`, it checks the
newest backup in the configured directory. Select the backup's namespace with
`--version` when applicable.

## Versioned transfers

Use the same `--version` for backup and restore.

```bash
loaf --version v1-2-3 backup
loaf --version v1-2-3 restore --backup-dir /media/transfer
```

The folder name is `YYYYMMDD-HHMM_v1-2-3-local-inference_local-ai-ferry`.
Restore selects the newest backup for the requested version when searching a
backup directory. Inside a specific folder, it uses the saved version. It
checks the saved image tags before loading images or extracting volumes. Only
`source`, `installers` and `models` restore into another version.
[Launcher options](launcher.md#run-a-versioned-deployment)

## Check saved Modelfiles during backup

A backup with `models` checks each saved Modelfile against Ollama's current
definition for the same model. It uses the offline Ollama image in a
network-disabled container with the model volume mounted read-only. Backup
warns about differing definitions, missing referenced blobs, and
definitions it cannot check. It writes the archive with the captured files.

The normal offline transfer sequence is `loaf backup`, `loaf restore`,
then `loaf offline`. Offline startup applies saved Modelfiles, so a
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
./loaf offline
```

Use the account that created the backup when copying it. New backups are
readable and writable by their owner on filesystems that enforce Unix permissions.

## Debian restore

Copy the complete trusted backup folder to the destination, or attach the
media that holds it. Use a login with Docker access. On Debian 12 or 13, use
`./loaf setup --with-docker-deps` in place of the first setup command when
installing Docker from the backup's matching Debian package cache.
[Docker access](debian.md#docker-access) · [Disconnected Debian installation](installation.md#disconnected-debian-installation)

```bash
cd /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
./loaf setup
./loaf restore
cd ~/.local-ai-ferry
./loaf offline
```

Run inside a backup folder without `--project-dir`, setup and restore use
the project `~/.local-ai-ferry`. Setup prepares it and copies the launcher to
`~/.local-ai-ferry/loaf`. The backup folder stays unchanged, and loaf refuses a
backup folder as `--project-dir`. To use another project, pass
`--project-dir PATH`, or run the backup's `loaf` by its full path from that
project directory.

To recover a source checkout, restore `source`, or everything, into a new
project directory. Restore adds the archived files, including
`python-package/`, `docs/` and the Git history, without replacing existing
files. Backups leave the host-specific virtualenv behind. Run
`PYTHONPATH=python-package/src python3 -m loaf setup` in the project to
rebuild `.venv` from the restored wheels when you develop there.

```bash
./loaf restore --project-dir ~/src/local-ai-ferry
```

## Restore behavior

Restore checks the archive's structure and every member's hash before
calling Docker. For a backup folder, it also checks the launcher and the
archive against `SHA256SUMS`. It checks volume paths and archive links before
extracting volumes, and refuses volumes in use. Restore applies the selected
types in this order: `source`, `config`, `installers`, `images`, `models`,
`webui`, `chats`. Restoring `config` keeps the backup's deployment version.
Restoring `chats` creates the account and chat volumes with their ownership
labels, loads the chats' package images and writes the bearer key with
owner-only permissions. It refuses a different existing key.

Restore starts services only when Modelfile replay is enabled. On a
recovered host, run `loaf offline` and open the printed address to sign in
or create the first account.

Add `--import` or set `import_models = true` under `[transfer]` to start offline
Ollama and import all saved Modelfiles after restoring `models`.
The default uses NVIDIA container access. Add `--cpu` to run the import on CPU.
This step leaves WebUI stopped. Run
`loaf offline` to start WebUI. If import fails, the restored data remains in
place. Fix the problem and start the desired serving mode. Use `--no-import`
to skip a configured import.

Keep model files in Linux named volumes. Paths can include colons.
