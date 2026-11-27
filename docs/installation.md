# Installation

## Disconnected Debian installation

Copy the trusted backup folder to the destination and enter it. The
folder's `loaf` uses its bundled Python. Setup and restore make the folder
the project and move the backup to its `backup/` directory. Prepare a compatible NVIDIA driver
for GPU inference and use an account with Docker access.
[Backup recovery](transfer.md#debian-restore) · [Docker access](debian.md#docker-access)

```bash
chmod +x loaf
./loaf setup
./loaf restore
```

Setup creates default configuration and the terminal server key and links
the packaged Docker files. Restore checks the backup and loads everything it
contains: settings and Modelfiles, installers, images, models and WebUI
data, and, when included, the source checkout and chat folders. Continue
with `./loaf offline` in the same folder. For a fresh deployment, use the
release executable and run setup.
[Backup recovery](transfer.md#debian-restore) ·
[Release executable](releases.md#run-the-executable)

On Debian 12 or 13, use the following setup command when the backup has
Docker installers for the same release and package architecture:

```bash
./loaf setup --with-docker-deps
```

The command verifies and extracts the backed-up Debian packages and uses
the cached local APT source. When available, it also uses the cached
NVIDIA toolkit. For a source checkout with host Python, see the separate
editable installation steps. [Source installation](launcher.md#prepare-python-installation-offline)

After a fresh Docker install, enable it and check your login has access.
[Docker access](debian.md#docker-access)

```bash
sudo systemctl enable --now docker
```

If Docker has yet to be configured for NVIDIA, register the runtime and restart
Docker when it is safe to interrupt running containers.

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

If the archive omitted the Open Terminal bearer key and setup created a new
one, update the saved terminal connection after starting WebUI.
[Terminal setup](terminal-server.md#enable-and-connect)

## Debian package

On Debian 12 or 13, install the release's `loaf_<version>_all.deb` with apt.
It uses the system Python and installs `loaf` as a normal Debian
application. Debian 13 provides Docker from its archive. Debian 12 needs
Docker's repository for Engine 26 or newer and Compose 2.26.1 or newer.
[Docker installation](debian.md#install-docker-from-dockers-repository)

```bash
sha256sum --check --ignore-missing SHA256SUMS
sudo apt install ./loaf_0.2.2_all.deb
loaf version
```

To build the package from a source checkout, run `loaf package debian`.
[Local builds](releases.md#build-and-check-locally)

The package operates the system deployment. Its settings are in `/etc/loaf`
and its data, backups and lock are in `/var/lib/loaf`. Root, members of the
`docker` group and the `loaf` systemd service can operate it. Its backups in
`/var/lib/loaf/backup` are readable by that group, so any member can restore
them. The terminal server key is `/etc/loaf/terminal-server.key`. Select a portable deployment
directory with `loaf --project-dir PATH`. A portable deployment reads
`/etc/loaf` below its own `config/config.ini`.
[Settings layers](configuration.md#project-configuration)

To restore a copied backup into the system deployment, run
`sudo loaf restore --archive PATH` with the backup folder. Settings in
`/etc/loaf` need root. Restored settings, installers and the terminal server
key stay readable by the `docker` group, and the key stays at
`/etc/loaf/terminal-server.key`.
[Restore behavior](transfer.md#restore-behavior) Start offline serving at
boot after building or restoring the images:

```bash
sudo systemctl enable --now loaf.service
```

`man loaf` and `loaf docs` show the documentation for the installed release.
The package also installs Bash completion for `loaf`, which bash-completion
loads in new shells. [Bash completion](launcher.md#bash-completion)
Purging the package removes the generated key and empty directories. Backups
and the Docker volumes with models and chats remain.

## Host integrations

### systemd

Generate the system-wide unit using the installed `loaf` under the account
that will run the service. That account needs Docker access. The unit uses
the selected project, Python environment, override directory and namespace.
Like the Debian package's unit, it requires `docker.service` and starts after
it. `ExecStart` runs `loaf offline --attach`, which follows logs in the
foreground after startup. Stopping the unit runs `loaf stop` for the same
deployment and removes its service containers. Both units allow 90 seconds
for that, because the terminal server may take 45 seconds to stop its chat
containers.
[Docker access](debian.md#docker-access) · [systemd service options](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)

```bash
./loaf integration systemd > open-webui.service
systemd-analyze verify ./open-webui.service
sudo install -m 0644 open-webui.service /etc/systemd/system/open-webui.service
sudo systemctl daemon-reload
sudo systemctl enable --now open-webui.service
```

### Continue for VS Code

Generate YAML for the selected model list and WebUI address. Use `--models-file`
to choose another model INI.

```bash
./loaf integration continue > continue.yaml
```

Enable **API Keys** under **Settings → Authentication** in the Open WebUI
admin area. For a non-admin account, grant the **API Keys** feature permission.
Under **Settings → Account**, click **Show** next to **Secrets** and create a
key. Replace each `REPLACE_WITH_OPEN_WEBUI_API_KEY` value before merging the
YAML into your Continue configuration. Each model gets Continue's default
roles, `chat`, `edit`, `apply` and `summarize`, and Continue detects its
capabilities. The YAML's opening comment says what to check on the model card
before you widen them: `autocomplete` needs a fill-in-the-middle code model,
`embed` and `rerank` need embedding and reranker models, and the commented
`capabilities` line adds `tool_use` or `image_input` only for models that
support them. Keep the key out of
version control. The generated model IDs use Ollama aliases when configured.
Download those models before using Continue.
[Open WebUI API keys](https://docs.openwebui.com/features/authentication-access/api-keys/) · [Continue OpenAI provider](https://docs.continue.dev/customize/model-providers/top-level/openai)

WebUI defaults to loopback port 3000. When connecting through an SSH forward,
set `apiBase` to the forwarded address with the `/api` suffix.
[SSH forwarding example](debian.md#verify-and-open-webui)

### Open WebUI Tether

Run `loaf integration tether` for the download link and a short guide to
installing the desktop client and connecting it to the running deployment.
[Open WebUI Tether installation](tether.md)
