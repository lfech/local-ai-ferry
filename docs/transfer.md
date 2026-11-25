# Backup and transfer

[Project overview](../README.md)

Run `loaf backup` on the source and copy the entire dated backup folder to the
destination. The folder supplies the standalone Linux amd64 launcher. Run
`./loaf setup` and `./loaf restore` inside the folder.
Use `./loaf setup --all` to recover the full checkout and any included Docker data from a
backup made from source. [Debian restore](#debian-restore)

## Transfer models between existing deployments

Use `--models-only` on the source and destination. The `loaf export` and
`loaf import` commands use the same model-store workflow.
Both commands stop the selected deployment's downloader. The online or
offline serving profile can stay running if its Ollama container is the only
running consumer of the volume. Use `--version` to select the deployment volume.

```bash
loaf backup volumes --models-only --archive /media/transfer/models.tar.gz
```

```bash
loaf restore volumes --models-only --archive /media/transfer/models.tar.gz
```

Without `--archive`, export writes `backup/YYYYMMDD-HHMM_loaf-volumes.tar.gz`
and import selects the newest model archive in that directory. Use
`--backup-dir PATH` to select a different directory. The archive contains
Ollama blobs, manifests and saved Modelfiles and is portable across deployment
namespaces. Use a destination Ollama that reads the archived manifest format.
WebUI data, images and the checkout travel in a full backup.

Export and import support both legacy manifests and Ollama's
`manifests-v2` registrations. V2 tags can be relative links to
content-addressed manifest blobs. The archive check verifies those links,
the manifest blobs' SHA-256 hashes, and the presence and sizes of
referenced model blobs before import changes the destination volume.
[Ollama manifest storage](https://github.com/ollama/ollama/blob/v0.40.17/manifest/manifest.go#L1169)

Both commands use `rsync` in a disposable container without networking.
Build or upgrade the downloader image while connected to obtain `rsync`.
Allow space for an uncompressed tar on the host and for a staged copy in
temporary Docker storage. Keep the model catalog stable during transfer
by avoiding model pulls, creation and deletion through the Ollama API.

Import copies missing blobs first and updates matching manifests and
Modelfiles. It preserves destination-only data by default. Use
`loaf restore volumes --models-only --delete --archive PATH` to remove blobs, manifests and Modelfiles
absent from the archive. This can remove models currently in use. Use
`--dry-run` to check which deployment and archive you selected. To avoid
interrupting requests for removed models, stop inference before mirroring
a store. Start the same serving profile again after the copy.

## Select deployment components

Use any combination of `images`, `volumes` and `project` after `backup` or
`restore`. With no component names, backup saves all three and restore
recovers everything present in the archive.

```bash
loaf backup images project
loaf restore images project --archive /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
```

Restore verifies every payload before applying the selected components.
It reports any explicitly requested component missing from the archive.
Only a backup that includes `volumes` stops the project services. Restoring
volumes without images requires the matching local `local/ollama-online`
helper image. Restoring `project` recovers settings and Modelfiles. Use
`./loaf setup --all` for the source checkout from a bundle containing
`project`. [Debian restore](#debian-restore)

## Archive contents

Each deployment backup is a folder named `YYYYMMDD-HHMM_local-ai-ferry`.
Deployment backups and model-store archives use separate formats and are selected by
timestamp. Use `--archive PATH` to select a specific backup folder or archive.

```text
backup/YYYYMMDD-HHMM_local-ai-ferry/
  loaf
  backup.json
  YYYYMMDD-HHMM_local-ai-ferry.tar.gz
```

`backup.json` records the archive name, deployment version and executable
checksum. In a source checkout, use a connected machine. Backup builds
`dist/loaf` from the current working files before accessing Docker and
includes the result in the folder. A standalone deployment includes the
binary that launched it. [Local binary build](releases.md#build-and-check-locally)

| Archive member | Contents |
| --- | --- |
| `volumes.tar` | Ollama and Open WebUI volumes, plus the selected deployment's terminal workspace volumes when included. |
| `images.tar` | The three core Docker images and the matching Open Terminal image when available locally. |
| `project.tar` | Saved configuration, Modelfiles and available installer files. Backups made from a source checkout also include its working files, Git history and virtualenv. |
| `terminal.json` | Optional terminal workspace inventory and bearer key included with `volumes`. |
| `sha256.json` | SHA-256 digests of all payloads. |

The default directory is `backup/` in the project. Set `[transfer]`
`backup_dir` in `config.ini` for a persistent choice or pass
`--backup-dir PATH` to either command. An explicit relative path resolves
from the shell's current directory. Backup names use local time.
Restore uses the folder it is run from or the folder containing the executable.
Use `loaf restore --archive PATH` to choose a specific folder. Restore can
also select the latest backup from the configured directory for the requested
namespace. Git ignores the default `backup/` directory and the generated names.

Keep the transfer folder private. The archive contains WebUI data and may
include Git history. A deployment prepared by the standalone executable can
back up its configuration and cached installers directly. The release
executable supplies the packaged defaults and Docker files. Copy required
`.env` settings and other external data separately.

The images component includes the Open Terminal image tagged for the selected deployment
when it is available locally. Use `loaf backup --no-terminal-server` to skip
that image. Set `[terminal_server] include_in_backup = true` to add per-account
`user` volumes and the bearer key when backing up volumes. Move any
chat files you want to transfer into `/home/user` before backing up.
`loaf restore` reads the archive to decide which assets to restore. The
checkout payload excludes the configured terminal key file. Open WebUI's
saved connection and its credential can be present in `volumes.tar` with
either setting. [Terminal server transfer](terminal-server.md#offline-transfer-and-shutdown)

Source checkout backup includes the launcher wheel, dependency wheels and wheel
cache record in `project.tar`. It reuses a current cache and refreshes it with package
indexes disabled when the package source or Python environment changes. Run
`loaf install` while connected to populate the cache before your first backup
from Debian.
[Python wheel preparation](launcher.md#prepare-python-installation-offline)

On Debian, run `loaf setup` while connected to fill `install-cache/deb/nvidia/`.
Backup includes packages matching the installed NVIDIA toolkit or a complete
prepared set for the host architecture. On Debian 13, add
`--with-docker-deps` to include Docker's cached `.deb` files and local APT
index under `install-cache/deb/docker/`. Use compatible wheels and Debian
packages on the destination. [Disconnected installation](installation.md#disconnected-debian-installation)

Backup stops all project services before capturing the volumes and leaves them
stopped. More than one backup in a minute gets numbered suffixes such as `-2`.
Allow room for the uncompressed payloads and compressed archive during backup.
Restore stages the payloads in the writable project directory. The backup
can stay on read-only media.

Both commands report per-payload progress on standard error. Interactive
terminals show a live bar and ETA when the total is known. Docker exports
show bytes transferred, rate, and elapsed time while they stream. Redirected
logs get periodic updates and a final line for each stage. While Docker prepares
an image export or pauses its stream, the meter shows `waiting for Docker`.

## Verify a backup

```bash
loaf backup --verify --archive /media/transfer/YYYYMMDD-HHMM_local-ai-ferry
```

Verification checks the bundled executable and each archive payload by SHA-256,
lists the available components, and reports the operating system and
architecture of any saved images. It performs no Docker operations. Allow
temporary disk space when the archive contains `images.tar`. Without `--archive`,
it checks the newest backup in the configured directory. Select the backup's
namespace with `--version` when applicable. Verification and restore both
require the SHA-256 manifest in the archive.

## Versioned transfers

Use the same `--version` for backup and restore.

```bash
loaf --version v1-2-3 backup
loaf --version v1-2-3 restore --backup-dir /media/transfer
```

The folder name is `YYYYMMDD-HHMM_v1-2-3-local-inference_local-ai-ferry`.
Restore selects the newest backup for the requested version when searching a
backup directory. Inside a specific folder, it uses the saved version. It checks the
saved image tags before loading images or extracting volumes, including when
you choose a file with `--archive`. [Launcher options](launcher.md#run-a-versioned-deployment)

## Check saved Modelfiles during backup

Plain `loaf backup` checks each saved Modelfile against Ollama's current
definition for the same model. It uses the offline Ollama image in a
network-disabled container with the model volume mounted read-only. Backup
warns about differing definitions, missing referenced blobs, and
definitions it cannot check. It writes the archive with the captured files.

The normal offline transfer sequence is `loaf backup`, `loaf restore`,
then `loaf offline`. Offline startup applies saved Modelfiles, so a
reported difference can affect the model that starts on the destination.

## Export cached models during backup

Use `loaf backup --export` to refresh the selected cached Modelfiles before
snapshotting the volumes. It uses the same model selection as `loaf download`.
Pass `--models-file PATH` to choose a different INI. All selected models must
already be cached. The export recopies configured aliases from their cached
sources before saving the Modelfiles. If export fails,
no new archive is published. Set `export_models = true` under `[transfer]`
to enable the step by default. `--no-export` skips the configured refresh
and still checks the saved definitions.

## Debian backup

```bash
./loaf backup
./loaf offline
```

Use the account that created the archive when copying it. New archives are
readable and writable by their owner on filesystems that enforce Unix permissions.

## Debian restore

Copy the complete trusted backup folder to a writable directory on the
destination. Use a login with Docker access. On Debian 13, use
`./loaf setup --with-docker-deps` in place of the first setup command when
installing Docker from the backup's matching Debian package cache.
[Docker access](debian.md#docker-access) · [Disconnected Debian installation](installation.md#disconnected-debian-installation)

```bash
cd ~/YYYYMMDD-HHMM_local-ai-ferry
chmod +x loaf
./loaf setup
./loaf restore
./loaf offline
```

To recover the source checkout and Docker data together, enter the copied
folder from a source checkout backup and run `./loaf setup --all`. The
command recovers the archived files, including `python-package/`, `docs/`,
Git history and cached installers. As with a minimal restore, Docker
volumes must be new or empty. The archived virtualenv retains references to
the source machine. Use `./loaf` as the launcher on the destination.

After an in-place restore, the backup archive, launcher copy and manifest
are in `backup/`. The root launcher points to `dist/loaf` in both restore
modes. A full checkout restore also adds the source files shown below.

```text
YYYYMMDD-HHMM_local-ai-ferry/
  backup/
    YYYYMMDD-HHMM_local-ai-ferry.tar.gz
    loaf
    backup.json
  loaf -> dist/loaf
  dist/
    loaf
  python-package/
  docs/
```

Use `./loaf backup --verify --archive ./backup` to verify the retained
bundle. The destination `backup/` and `dist/loaf` paths must be available
before the in-place restore.

To restore from read-only media, run the backup's `loaf` by its full path
from a separate writable project directory. The executable locates the
archive beside itself and leaves the original bundle on the media.

## Restore behavior

Restore discovers the archive and executable from the backup folder and checks
their presence, the executable hash, the archive structure and the recorded
payload hashes before calling Docker. Restore checks volume paths and
archive links before extracting selected volumes. Stop containers that use
the destination volumes before restoring them. Restore loads selected images,
extracts selected volumes into new or empty volumes, and writes saved settings
and Modelfiles to `config/` when restoring the project. The bundled executable
supplies the launcher and deployment files.
Restore leaves services stopped by default. Run `loaf offline` and open the
printed address to sign in or create the first account.

Add `--import` or set `import_models = true` under `[transfer]` to start offline
Ollama and import all saved Modelfiles after restoring volumes.
The default uses NVIDIA container access. Add `--cpu` to run the import on CPU.
This step leaves WebUI stopped. Run
`loaf offline` to start WebUI. If import fails, the restored data remains in
place. Fix the problem and start the desired serving mode. Use `--no-import`
to skip a configured import.

Keep model files in Linux named volumes. Paths can include colons.
