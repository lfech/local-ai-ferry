# Installation

[Project overview](../README.md)

Three channels deliver the same `loaf` launcher: a source checkout for
development, the standalone executable for portable deployment directories,
and the Debian package for the system deployment. Each runs Docker Compose
with the deployment files of its own installation. Prepare the host first:
Docker, Docker access for your login and, for GPU inference, the NVIDIA
driver.

[Debian host setup](debian.md)

## Source checkout

The installable project is [python-package](../python-package/pyproject.toml),
with Python source under `python-package/src/loaf`. Its distribution name is
`local-ai-ferry`, and its console command is `loaf`. The package ships
the Compose project and configuration defaults. The test suite lives in
`python-package/tests/` in the checkout.

```bash
PYTHONPATH=python-package/src python3 -m loaf install
./loaf --help
```

After source setup, run `source .venv/bin/activate` or call `./loaf` directly. Source setup
links `./loaf` to `.venv/bin/loaf` unless that path exists, so a checkout uses
the same command as standalone and restored deployments. Source setup runs
under the checkout lock; run it as the checkout's owner, because the APT
steps use `sudo`.

The runtime has no third-party dependencies. Setuptools is a build dependency.
An [editable pip install](https://pip.pypa.io/en/stable/topics/local-project-installs/#editable-installs)
links to the source directory; reinstall after moving the checkout or changing
package metadata. The `loaf` command and `python -m loaf` call the same launcher,
registered through a [console entry point](https://setuptools.pypa.io/en/latest/userguide/entry_point.html#console-scripts).

`loaf --help` lists commands and global flags. Use `loaf COMMAND --help` for
command flags. On Debian, use `./loaf` with a login that has
Docker access.

[What setup prepares](cli-help.md#setup) · [Command reference](cli-help.md) ·
[Docker access](debian.md#docker-access)

### Prepare Python installation offline

On Debian, run `loaf setup` or `loaf install` to prepare the launcher wheel and
its declared build and runtime requirements in `install-cache/wheels/`. The
bootstrap, setup and backup reuse the cache when it matches the checkout and
Python environment. Setup tries local wheels first and fetches missing
requirements through pip's configured sources. Older caches without a cache
record are refreshed once.

Setup installs the checkout in editable mode from the local wheel directory.
Repeat setup reuses a working editable installation with current package
metadata. Python preparation prints short status messages and shows pip's
output when the step fails. Backup bundles the wheels and their cache record
for recovery.

After recovering a Debian checkout, run the same bootstrap command. A
complete wheel directory for the selected Python lets pip work offline.

```bash
PYTHONPATH=python-package/src python3 -m loaf install
./loaf --help
```

The launcher and its build backend use pure-Python wheels. The destination
Python must meet the packages' version requirements. To install the recovered
launcher wheel into an existing virtualenv, activate that environment and run:

```bash
python -m pip install --no-index --find-links install-cache/wheels local-ai-ferry
```

Outside a source checkout, `loaf setup` from a wheel installation prepares a
deployment like the standalone executable. The release wheel carries the
standalone executable that backups include; a wheel built from the checkout
does not. Use the source checkout for host setup when installing from a wheel.

[Backup and transfer](transfer.md) · [Release executable setup](#standalone-executable)

### Test packages

The terminal server API tests need `aiohttp`, `httpx` and Open Terminal.
Run `loaf setup tests` with the virtualenv that runs `loaf test`. It
requires Python 3.11 or newer and a source checkout. It caches the pinned
packages from
[`tests/requirements.txt`](../python-package/tests/requirements.txt) and
their requirements as wheels in `install-cache/wheels/`, with a separate
cache record. Then it installs them from that directory only.

```bash
./loaf setup tests
```

Repeat runs reuse the cache when the pins and Python are unchanged. Setup
tries local wheels first and fetches missing packages through pip's
configured sources. Backups from the checkout carry the test wheels and
their record. A recovered checkout with the same Python version and
platform can install them offline. Some wheels are platform-specific.
`loaf clean --full` asks before it removes them with the rest of the install cache.

[pip local install reference](https://pip.pypa.io/en/stable/cli/pip_install/#examples)

## Standalone executable

Download `loaf`, `LICENSE.txt`, and `SHA256SUMS` from Releases. From the
download directory, verify the downloads and set up a project.

```bash
sha256sum --check --ignore-missing SHA256SUMS
chmod +x loaf
mkdir loaf-project
cd loaf-project
../loaf setup
```

The executable supplies CPython, the launcher, deployment assets, and
the Python build requirements. It unpacks its runtime on first use under
`${XDG_DATA_HOME:-$HOME/.local/share}/loaf/runtime/`. The executable
recreates the runtime after `loaf clean` and uses `flock` to coordinate
cleanup with other invocations. Generated systemd units refer to the
original executable.

Continue with the [Quick Setup](../README.md#quick-setup) for a fresh
deployment, or restore a backup as [Debian restore](transfer.md#debian-restore)
shows.

[Python Build Standalone release](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110) ·
[What setup prepares](cli-help.md#setup) · [Debian setup](debian.md)

## Debian package

On Debian 12 or 13, install the release's `loaf_<version>_all.deb` with apt.
It uses the system Python and installs `loaf` as a normal Debian
application. Debian 13 provides Docker from its archive. Debian 12 needs
Docker's repository for Engine 26 or newer and Compose 2.26.1 or newer.

```bash
sha256sum --check --ignore-missing SHA256SUMS
sudo apt install ./loaf_0.5.0_all.deb
loaf version
```

To build the package from a source checkout, run `loaf package debian`.

The package operates the system deployment. Its settings are in `/etc/loaf`
and its data, backups and lock are in `/var/lib/loaf`. Root, members of the
`docker` group and the `loaf` systemd service can operate it. Its backups in
`/var/lib/loaf/backup` are readable by that group, so any member can restore
them. The package carries the standalone executable that each backup folder
includes. The package creates the terminal server key
`/etc/loaf/terminal-server.key`. Select a portable deployment
directory with `loaf --project-dir PATH`. A portable deployment reads
`/etc/loaf` below its own `config/config.ini`.

To restore a copied backup into the system deployment, run
`sudo loaf restore --archive PATH` with the backup folder. Settings in
`/etc/loaf` need root. Restored settings, installers and the terminal server
key stay readable by the `docker` group, and the key stays at
`/etc/loaf/terminal-server.key`.
Start offline serving at
boot after building or restoring the images:

```bash
sudo systemctl enable --now loaf.service
```

`man loaf` and `loaf docs` show the documentation for the installed release.
The package also installs Bash completion for `loaf`, which bash-completion
loads in new shells.
Purging the package removes the generated key and empty directories. Backups
and the Docker volumes with models and chats remain.

[Docker installation](debian.md#install-docker-from-dockers-repository) ·
[Local builds](releases.md#build-and-check-locally) ·
[Settings layers](configuration.md#project-configuration) ·
[Restore behavior](transfer.md#restore-behavior) · [Bash completion](launcher.md#bash-completion)

## Disconnected Debian installation

Copy the trusted backup folder to the destination and restore it there, as
[Debian restore](transfer.md#debian-restore) shows. The folder's `loaf`
brings its own Python. For a fresh deployment, use the
[release executable](#standalone-executable) instead.

The destination needs Docker access for your login and, for GPU inference, a
compatible NVIDIA driver. On Debian 12 or 13, `./loaf setup all docker-deps toolkit`
installs Docker and the NVIDIA toolkit from a backup that holds their
installers for the same release and architecture. After a fresh Docker
install, enable it; register the NVIDIA runtime once, when it is safe to
restart Docker.

```bash
sudo systemctl enable --now docker
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

[Docker access](debian.md#docker-access) ·
[Source installation](#prepare-python-installation-offline)
