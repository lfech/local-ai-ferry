# Host integrations

[Project overview](../README.md)

`loaf integration NAME` prints what connects a deployment to its host and to
desktop tools: a systemd unit, Continue YAML or the Tether installation steps.

[integration commands](cli-help.md#integration)

## systemd

Generate the system-wide unit using the installed `loaf` under the account
that will run the service. That account needs Docker access. The unit uses
the selected project, Python environment, override directory and namespace.
Like the Debian package's unit, it requires `docker.service` and starts after
it. `ExecStart` runs `loaf run offline --attach`, which follows logs in the
foreground after startup; generate it with `--cpu` for a host without NVIDIA
GPU access. For the Debian package's system deployment on such a host,
install `loaf --system integration systemd --cpu` as
`/etc/systemd/system/loaf.service`, which replaces the packaged unit. Stopping the unit runs `loaf stop` for the same
deployment and removes its service containers. Both units allow 90 seconds
for that, because the terminal server may take 45 seconds to stop its chat
containers.

```bash
./loaf integration systemd > local-ai-ferry.service
systemd-analyze verify ./local-ai-ferry.service
sudo install -m 0644 local-ai-ferry.service /etc/systemd/system/local-ai-ferry.service
sudo systemctl daemon-reload
sudo systemctl enable --now local-ai-ferry.service
```

[Docker access](debian.md#docker-access) ·
[systemd service options](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)

## Continue for VS Code

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

WebUI defaults to loopback port 3000. When connecting through an SSH forward,
set `apiBase` to the forwarded address with the `/api` suffix.

[Open WebUI API keys](https://docs.openwebui.com/features/authentication-access/api-keys/) ·
[Continue OpenAI provider](https://docs.continue.dev/customize/model-providers/top-level/openai) ·
[SSH forwarding example](debian.md#verify-and-open-webui)

## Open WebUI Tether

Tether is a desktop app that connects to a running Open WebUI deployment.
Install it on the computer where you want to use it. For example, if `loaf`
runs in Debian under WSL2, install Tether on Windows.

[Official Tether downloads](https://github.com/open-webui/tether#download)

### Install the desktop app

Run `loaf integration tether` for a short installation guide and the download link.

Use a connected computer to download the installer for your operating system
and processor. For a disconnected desktop, transfer the installer and prepare
any required system packages before disconnecting.

- **Debian, Ubuntu or Linux Mint** — Run `dpkg --print-architecture` and
  download the matching `.deb`. From the download directory install it
  with APT. On `amd64`, use the following command.

  ```bash
  sudo apt install ./tether_amd64.deb
  ```

  On `arm64`, use `./tether_arm64.deb`. Launch **Open WebUI Tether**
  from your desktop application menu.
- **Windows** — Download the x64 or ARM64 setup executable, run it, and
  open **Open WebUI Tether** from the Start menu.
- **macOS** — Download the Apple Silicon or Intel disk image, open it, move
  **Open WebUI Tether** to Applications, and launch it.

[Official release assets](https://github.com/open-webui/tether/releases)

### Connect to your deployment

1. Start the prepared deployment with `./loaf run offline` or
   `./loaf run online` and note the printed WebUI address.
2. Open the address in a browser on the Tether computer and confirm you can
   sign in. For a local deployment the default is `http://localhost:3000`.
   From another computer, forward the port using the
   [SSH example](debian.md#verify-and-open-webui) and use the local end of
   that tunnel in Tether.
3. In Tether, enter that address under **Connect to Open WebUI** and sign
   in with your WebUI account. Choose **Not now** at the optional Open
   Terminal prompt for chat-only use.
4. Select one of the deployment's models and send a short test message.
   If you cannot connect, check the address and run `./loaf status`
   on the serving host. If a Tether feature needs a newer WebUI build,
   see [image upgrades](launcher.md#modes) and the publisher's release notes.

[Open WebUI connection walkthrough](https://docs.openwebui.com/ecosystem/tether/#get-started)

### Optional desktop terminal

To use the desktop computer's own files and commands in WebUI, choose
**Install and connect** at the Open Terminal prompt. Keep Tether running
and select your computer under **Your devices** in the WebUI chat's
terminal menu. Prepare this optional component while connected. On
supported macOS and Windows systems, choose **Restrict file access** under
**Settings → Open Terminal → Sandbox**, select a folder, and choose
**Apply and restart**. Wait for **Sandbox active**. On Linux, use the Compose
deployment's per-account workspaces if you need an isolated terminal.

[Official Tether sandbox documentation](https://docs.openwebui.com/ecosystem/tether/#give-it-a-folder-to-work-in) ·
[Per-account server workspaces](terminal-server.md)
