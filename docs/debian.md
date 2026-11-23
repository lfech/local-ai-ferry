# Debian Linux setup

[Project overview](../README.md)

Run commands from the project directory unless a step says otherwise.

Use Debian 12 or 13, amd64, with Python 3.10 or newer. The default serving
mode requires a compatible NVIDIA GPU. A preparation host can serve on CPU
with `loaf online --cpu` or `loaf offline --cpu`. [CPU serving](launcher.md#serve-on-cpu)
These steps use the native, system-wide Docker Engine. Use `sudo` for package
installation, runtime configuration and service administration.

## Debian in WSL2

On Windows, use a Debian WSL2 distribution with systemd enabled. For GPU
inference, install an NVIDIA Windows driver that supports WSL2, then run
`nvidia-smi` inside Debian to check GPU passthrough. Install Docker Engine
inside that distribution. The native Debian GPU driver installation below
applies to a bare-metal Debian host. [Microsoft WSL systemd guide](https://learn.microsoft.com/en-us/windows/wsl/systemd) · [NVIDIA CUDA on WSL](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)

## Prepare Debian before disconnecting it

For GPU inference on native Debian, first install a compatible NVIDIA driver using the
[NVIDIA Debian driver guide](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/latest/debian.html),
reboot, and confirm that this command lists the GPU:

```bash
nvidia-smi
```

On Debian 13, [install Docker with loaf](#install-docker-and-cache-its-packages).

### Install Docker from Docker's repository

The manual commands below use Docker's repository and apply to Debian 12 or
13. If Docker already works, check its Compose version and skip reinstalling
it. For package conflicts, follow the
[official Debian installation guide](https://docs.docker.com/engine/install/debian/).

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg python3 python3-venv
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Compose **2.30.0 or newer** supports the `gpus: all` setting. Compose
**2.26.1 through 2.29.x** can use the NVIDIA device reservation and produces a
compatibility warning in `loaf status`. The documented Compose 2.40.3 build
run used Buildx 0.20.1. Complete Docker and driver setup before disconnecting.
[Compose service options](https://docs.docker.com/reference/compose-file/services/#gpus) · [Compose GPU reservations](https://docs.docker.com/compose/how-tos/gpu-support/) · [Verification record](verification-record.md)

## Docker access

The project commands below assume that your login can access the Docker Engine
socket. If your account was just added to the `docker` group, log out and back
in so the next shell inherits the membership. Check the current shell with
`id -nG`. The group grants root-level control of the host.
[Docker Engine post-installation guide](https://docs.docker.com/engine/install/linux-postinstall/#manage-docker-as-a-non-root-user)

Once your session has Docker access, check the client, daemon and Compose versions:

```bash
docker version
docker compose version
```

## Install the loaf command

From the project root, run the installer with the Python you want to use.
This works before the `loaf` console command is installed.

```bash
PYTHONPATH=python-package/src python3 -m loaf setup
.venv/bin/loaf --help
```

If you select Python with pyenv, run the first command with that interpreter.
The bootstrap uses APT for missing venv support only when APT owns that
Python. It prepares `install-cache/wheels/`, installs the editable launcher
in `.venv` from the local wheels, and prepares the NVIDIA Toolkit installers
in `install-cache/deb/nvidia/`. Setup uses a complete local set for the host
architecture or fetches the missing installers on a connected host. Set up the
runtime on a GPU serving host if its Docker daemon has never been configured
for the toolkit.
[Python wheel preparation](launcher.md#prepare-python-installation-offline)

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Restart Docker when no containers need to remain running. Run ordinary
launcher commands as your regular user with [Docker access](#docker-access).
For offline recovery, see [Disconnected installation](installation.md#disconnected-debian-installation).
[NVIDIA installation guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

### Install Docker and cache its packages

On a connected Debian 13 host, run setup with the Docker flag:

```bash
PYTHONPATH=python-package/src python3 -m loaf setup --with-docker-deps
```

Setup caches Debian's `docker.io`, `docker-cli`, `docker-compose`,
`docker-buildx`, and their required packages under
`install-cache/deb/docker/trixie-<architecture>/`. It installs Docker
from that cache when the Engine, CLI, Compose, or Buildx is missing.
Use the same Debian release and package architecture on the target.
After installation, enable Docker with `sudo systemctl enable --now docker`
and give your login [Docker access](#docker-access).
[Debian Docker packages](https://packages.debian.org/trixie/docker.io) ·
[Offline installation](installation.md#disconnected-debian-installation)

## Use a prepared archive

Copy the complete backup folder to Debian and enter it. Run `./loaf setup`
and `./loaf restore` to load the images, volumes and saved settings.
Then start or resume inference. [Debian restore](transfer.md#debian-restore)

```bash
./loaf offline
```

Backend startup applies saved Modelfiles automatically. For a model-only
archive, use `.venv/bin/loaf import --archive PATH` on an existing deployment.
[Model transfer](transfer.md#transfer-models-between-existing-deployments)

## Verify and open WebUI

Run the [Debian smoke checks](verification.md#debian-smoke-checks).

Open the address printed by the launcher, **http://localhost:3000** by default.
Sign in with the restored account, or create the first account if none exists.
Use [WebUI settings](configuration.md#webui-settings)
and the model from your list. The server default is 16384 context tokens;
explicit model/chat settings can override it.

From a Debian or WSL2 shell, open an SSH forward to a headless host.

```bash
ssh -N -L 3001:127.0.0.1:3000 user@debian-host
```

Keep that terminal open and browse to **http://localhost:3001**. Use a free local port for the forward.
To publish WebUI directly to the LAN, follow the [local network guide](local-network.md).

## Prepare models directly on online Debian

On a connected Debian host, choose your models in a local `config/models.ini`
or use the packaged selection. Prepare them after installing `loaf`.

```bash
.venv/bin/loaf build
.venv/bin/loaf download
.venv/bin/loaf offline
```

To try the 8 GB example, set `models_file = model-examples/models.8gb.ini`
in the `[launcher]` section of `config/config.ini` before downloading.
[Model selection](launcher.md#select-models) · [Quick Setup](../README.md#quick-setup)

No archive or restore is needed when this same host also runs inference. For
another computer, use [Debian backup](transfer.md#debian-backup).

## Stop and inspect

```bash
.venv/bin/loaf link-docker-project
(
    cd docker-project
    docker compose --profile offline exec ollama-offline ollama ps
    docker compose --profile offline logs --tail 50 ollama-offline
)
.venv/bin/loaf stop
```

The final command removes service containers. Collect their logs first or use
`.venv/bin/loaf stop -n` to keep them. Start again with `.venv/bin/loaf offline`.
Data remains in the volumes. [Stop options](launcher.md#stop-and-remove-containers)
