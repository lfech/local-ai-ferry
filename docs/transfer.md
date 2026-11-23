# Backup and transfer

[Project overview](../README.md)

Run `loaf backup` on the source and copy the entire dated backup folder to the
destination. The folder supplies the standalone Linux amd64 launcher. Run
`./loaf setup` and `./loaf restore` inside the folder.

## Transfer models between existing deployments

Use `loaf export` on the source and `loaf import` on the destination.
Both commands stop the selected deployment's downloader. The online or
offline serving profile can stay running if its Ollama container is the only
running consumer of the volume. Use `--version` to select the deployment volume.

```bash
loaf export --archive /media/transfer/models.tar.gz
```

```bash
loaf import --archive /media/transfer/models.tar.gz
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
`loaf import --delete --archive PATH` to remove blobs, manifests and Modelfiles
absent from the archive. This can remove models currently in use. Use
`--dry-run` to check which deployment and archive you selected. To avoid
interrupting requests for removed models, stop inference before mirroring
a store. Start the same serving profile again after the copy.

## Archive contents

Each full backup is a folder named `YYYYMMDD-HHMM_local-ai-ferry`.
Existing full backups and model archives from earlier releases remain
readable with `loaf restore`, `loaf backup --verify`, and `loaf import`.

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
| `terminal.json` | Optional terminal workspace inventory and bearer key. |
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

Backup includes the Open Terminal image tagged for the selected deployment
when it is available locally. Use `loaf backup --no-terminal-server` to skip
that image. Set `[terminal_server] include_in_backup = true` to add per-account
`user` volumes and the bearer key independently of the image. Move any
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

Verification checks the bundled executable and each archive payload by SHA-256 and reports the
operating system and architecture of each saved image. It performs no Docker
operations. Allow temporary disk space for `images.tar`. Without `--archive`,
it checks the newest backup in the configured directory. Select the backup's
namespace with `--version` when applicable. Older archives without recorded
hashes remain restorable and return a verification error.

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

To restore from read-only media, run the backup's `loaf` by its full path
from a separate writable project directory. The executable locates the
archive beside itself.

## Restore behavior

Restore discovers the archive and executable from the backup folder and checks
their presence, the executable hash, the archive structure and the recorded
payload hashes before calling Docker. Restore also checks volume paths and
archive links before extracting them. Stop containers that use the destination
volumes before restoring. Restore loads images and extracts volume data into
new or empty volumes. It then writes the saved settings and Modelfiles to
`config/`. The bundled executable supplies the launcher and deployment files.
Restore leaves services stopped by default. Run `loaf offline` and open the
printed address to sign in or create the first account.

Add `--import` or set `import_models = true` under `[transfer]` to start offline
GPU Ollama and import all saved Modelfiles after restoring the images and volumes.
This step requires working NVIDIA container access and leaves WebUI stopped. Run
`loaf offline` to start WebUI. If import fails, the restored data remains in
place. Fix the problem and start the desired serving mode. Use `--no-import`
to skip a configured import.

Keep model files in Linux named volumes. Paths can include colons.
