# Install Open WebUI Tether

Tether is a desktop app that connects to a running Open WebUI deployment.
Install it on the computer where you want to use it. For example, if `loaf`
runs in Debian under WSL2, install Tether on Windows.
[Official Tether downloads](https://github.com/open-webui/tether#download)

## Install the desktop app

Run `loaf integration tether` for a short installation guide and the download link.

Use a connected computer to download the installer for your operating system
and processor. For a disconnected desktop, transfer the installer and prepare
any required system packages before disconnecting.
[Official release assets](https://github.com/open-webui/tether/releases)

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

## Connect to your deployment

1. Start the prepared deployment with `.venv/bin/loaf offline` or
   `.venv/bin/loaf online` and note the printed WebUI address.
2. Open the address in a browser on the Tether computer and confirm you can
   sign in. For a local deployment the default is `http://localhost:3000`.
   From another computer, forward the port using the
   [SSH example](debian.md#verify-and-open-webui) and use the local end of
   that tunnel in Tether.
3. In Tether, enter that address under **Connect to Open WebUI** and sign
   in with your WebUI account. Choose **Not now** at the optional Open
   Terminal prompt for chat-only use.
4. Select one of the deployment's models and send a short test message.
   If you cannot connect, check the address and run `.venv/bin/loaf status`
   on the serving host. If a Tether feature needs a newer WebUI build,
   see [image upgrades](launcher.md#modes) and the publisher's release notes.
   [Open WebUI connection walkthrough](https://docs.openwebui.com/ecosystem/tether/#get-started)

## Optional desktop terminal

To use the desktop computer's own files and commands in WebUI, choose
**Install and connect** at the Open Terminal prompt. Keep Tether running
and select your computer under **Your devices** in the WebUI chat's
terminal menu. Prepare this optional component while connected. On
supported macOS and Windows systems, choose **Restrict file access** under
**Settings → Open Terminal → Sandbox**, select a folder, and choose
**Apply and restart**. Wait for **Sandbox active**. On Linux, use the Compose
deployment's per-account workspaces if you need an isolated terminal.
[Official Tether sandbox documentation](https://docs.openwebui.com/ecosystem/tether/#give-it-a-folder-to-work-in) · [Per-account server workspaces](terminal-server.md)
