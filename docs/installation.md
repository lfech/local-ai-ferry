# Installation

## Disconnected Debian installation

Copy the trusted backup folder to a writable location and enter it. The
folder's `loaf` uses its bundled Python. Prepare a compatible NVIDIA driver
for GPU inference and use an account with Docker access.
[Backup recovery](transfer.md#debian-restore) · [Docker access](debian.md#docker-access)

```bash
chmod +x loaf
./loaf setup
./loaf restore
```

Setup creates default configuration and the terminal server key and links
the packaged Docker files. Restore checks the handoff and loads saved images,
volumes, settings and Modelfiles. Use `./loaf setup --all` in place of these
two commands when recovering the full source checkout from a checkout backup.
For a fresh deployment, use the release executable and run setup.
[Full checkout recovery](transfer.md#debian-restore) ·
[Release executable](releases.md#run-the-executable)

On Debian 13, use the following setup command when the backup has Docker
installers for the same release and package architecture:

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

## Host integrations

### systemd

Generate the system-wide unit using the installed `loaf` under the account
that will run the service. That account needs Docker access. The unit uses
the selected project, Python environment, override directory and deployment
version. `ExecStart` runs `loaf offline --attach`, which follows logs in the
foreground after startup. Stopping the unit runs `loaf stop` for the same
deployment and removes its service containers.
[Docker access](debian.md#docker-access) · [systemd service options](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)

```bash
.venv/bin/loaf integration systemd > open-webui.service
systemd-analyze verify ./open-webui.service
sudo install -m 0644 open-webui.service /etc/systemd/system/open-webui.service
sudo systemctl daemon-reload
sudo systemctl enable --now open-webui.service
```

### Continue for VS Code

Generate YAML for the selected model list and WebUI address. Use `--models-file`
to choose another model INI.

```bash
.venv/bin/loaf integration continue > continue.yaml
```

Enable **API Keys** under **Settings → Authentication** in the Open WebUI
admin area. For a non-admin account, grant the **API Keys** feature permission.
Under **Settings → Account**, click **Show** next to **Secrets** and create a
key. Replace each `REPLACE_WITH_OPEN_WEBUI_API_KEY` value and check the
generated roles and capabilities against each model's supported tasks before
merging the YAML into your Continue configuration. Keep the key out of
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
