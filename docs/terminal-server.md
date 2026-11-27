# Per-user terminal server in Open WebUI

The optional terminal server runs on native Debian and Debian in WSL2 with
Docker Engine **26 or newer** and Compose **2.26.1 or newer**. Compose
2.30 and newer support the direct GPU setting for the serving stack. On Engine 26
and 27, each workspace uses an internal bridge with
`com.docker.network.bridge.inhibit_ipv4=true`. Engine 28 and newer use
Docker's `isolated` gateway mode. Both keep an IPv4 address off the host
bridge. [Bridge driver options](https://docs.docker.com/engine/network/drivers/bridge/#skip-bridge-ip-address-configuration) · [Docker Engine 28 release notes](https://docs.docker.com/engine/release-notes/28/)

## Selected design

The provisioner and every chat container start from the shared local image
built from MIT-licensed Open Terminal. Each chat gets its own container, and
each account one more for drafts before their first message. A container
keeps its `/home/chat` folder and the packages installed with `apt` while its
chat exists. One volume holds every account's `/home/user` folder, and a
container mounts only its own account's folder there. Users select one direct
Open Terminal connection in Open WebUI and use its terminal, files, and model
tools. Ollama continues to handle inference. [Open Terminal 0.17.20 license](https://github.com/open-webui/open-terminal/blob/v0.17.20/LICENSE) · [Open WebUI terminal connection](https://docs.openwebui.com/features/open-terminal/setup/connecting/)

## Enable and connect

Add or update these settings in `config/config.ini`. The checkout enables the
service. The packaged settings default to disabled. The default base image is
`ghcr.io/open-webui/open-terminal:0.17.20-slim`, configured as
`[images] terminal_server_image`. The connection and model
settings below were verified with Open WebUI v0.13.4. Set `[images] webui_image`
to a fixed release or digest to reproduce the setup.

```ini
[webui]
auth = true

[terminal_server]
enabled = true
secret_file = config/terminal-server.key
```

Setup creates the key at the configured path with owner-only permissions.
For an existing deployment that only needs a key, run
`loaf setup --terminal-server-key` from the project directory. Both commands
preserve an existing key. For a new deployment, run these commands while
connected.

```bash
loaf setup
loaf build
loaf offline
```

Set `secret_file` to another path if needed. Relative paths start at the
project directory. The file holds one bearer key of 32–512 printable ASCII
characters without spaces, optionally terminated by one LF or CRLF.
Setup and restore copy the archived bytes when creating a key from a backup.
Enter the characters before the line ending as the WebUI bearer credential.
Both the launcher and the
provisioner validate this format. Compose mounts the file
only in the provisioner. Run `loaf status` to check the key under
**Configuration** and the Open Terminal image under **Local images and GPU**
or **Local images** with `--cpu`.
The container list shows the provisioner's current health.
Keep the file outside Git. Full backups can include the saved connection record
in WebUI's own volume. To rotate the key, stop the deployment, replace the key
file, start the deployment, and update the WebUI connection.

In the Open WebUI admin **Settings → Integrations → Open Terminal**, add a
direct connection to `http://terminal-server:8000`. Choose **Bearer** authentication,
enter the same key, and restrict access to the intended users or groups.
Verify and save the connection. Users select it in the chat terminal menu.
The URL is reachable from Open WebUI on the internal Compose network.
[Connection setup](https://docs.openwebui.com/features/open-terminal/setup/connecting/)

## Upgrade an existing terminal deployment

Rebuild with `loaf build`, then run `loaf online` or `loaf offline`.
Use `[terminal_server]` in the INI and `http://terminal-server:8000` for the
saved connection in Open WebUI. Direct Compose uses
`LOAF_TERMINAL_SECRET_FILE` for a custom bearer key path.

Releases before v1.0.0 do not migrate terminal data. This release ignores the
per-account volumes, per-chat volumes and chat package images of earlier
releases. Copy the files you need from them, then remove them. List them with:

```bash
docker volume ls --filter label=loaf.tool-server.workspace
docker volume ls --filter label=loaf.terminal.chat-project
docker image ls local/loaf-terminal-chat
```

## Use the terminal from a model

In **Workspace → Models**, enable **Terminal** for the model and set
**Function Calling** to **Default** or **Native**. Select **Open Terminal**
in the chat, or choose it as the model's **Default terminal**.
[Open WebUI model settings](https://docs.openwebui.com/features/workspace/models/)

If a follow-up after a terminal tool result fails with a role-order error,
enable the optional filter for that model.
[Conversation roles must alternate](troubleshooting.md#conversation-roles-must-alternate)

Use the **Working Directory** folder button beside the terminal selector
below the chat input. Select `user` or `chat` directly under **Recently Used**.
The bundled terminal supplies both as default choices alongside your recent
folders. Use **Open folder** to browse for another directory. If the terminal
selector is in the **+** menu, **Working Directory** is there too.
[Open WebUI working directory picker](https://docs.openwebui.com/features/open-terminal/file-browser/#working-directory)

A chat starts in `/home/chat`, and a draft in `/home/user`. Upload files from
the chat's **Files** tab or write them from the terminal. The **home**
breadcrumb in Files lets you browse both folders. The Files tab, your uploads
and model commands share the working directory you select.

| Folder | Path | Lifetime |
| --- | --- | --- |
| `user` | `/home/user` | Shared by all chats of your account and kept until an administrator removes it. It also holds your shell settings and other dotfiles. |
| `chat` | `/home/chat` | Private to the chat's container. It stays with the chat's installed packages while the chat exists, also while the container is stopped. Deleting the chat removes it. A temporary chat's folder is removed when it has been idle for `idle_seconds` and when the terminal server stops. |

Your commands and the model's commands run as root in the same container, so
both can change both folders and install packages with `apt`. Use
`/home/chat` for files to remove with the chat and `/home/user` for files to
keep. A draft's `/home/chat` belongs to the account's draft container, not to
the chat that the first message creates.
[Open WebUI file browser root](https://docs.openwebui.com/features/open-terminal/terminals/orchestration/file-browser-home-boundary/)

The provisioner uses the bundled Open WebUI deployment's SQLite database
to check saved chat ownership and deletion. It reads chat IDs and owners
through a read-only mount. Deleting a chat removes its container on the next
cleanup pass while the provisioner is running, normally within about five
seconds. Archiving a chat keeps its container. A custom deployment using
another database needs a compatible chat lookup for saved chats.

Ask Ministral to make one `run_command` call. This example puts the source
and executable in a new workspace folder. Expand the tool result in chat to
confirm execution and check for `Hello, World!`.

```text
Use Open Terminal's run_command once to execute this exact shell command. Wait for the result, then report its stdout.

dir=$(mktemp -d "$HOME/hello-check.XXXXXX") && cd "$dir" && printf '%s\n' '#include <stdio.h>' 'int main(void) { puts("Hello, World!"); return 0; }' > world.c && gcc -Wall -Wextra world.c -o world && ./world
```

## Workspace policy

All settings below belong to `[terminal_server]` in `config/config.ini`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `enabled` | `false` | Start the provisioner with `loaf online` and `loaf offline`. |
| `include_in_backup` | `false` | Include the `terminal` type in `loaf backup` without type names: the user volume and the bearer key. |
| `secret_file` | `config/terminal-server.key` | Source for the Compose bearer secret. |
| `apt_source_file` | blank | Use this APT source for terminal image builds and for `apt` in chat containers. [Package mirror](#package-mirror-for-image-builds) |
| `apt_repository_dir` | blank | Host directory for a `file:` repository, mounted read-only in chat containers. [Install packages in a chat](#install-packages-in-a-chat) |
| `network` | `isolated` | `isolated` lets chat containers reach only the provisioner. `outbound` also lets them reach every address the Docker host reaches. [Network access](#network-access) |
| `cpus` | `2` | CPU limit per container. |
| `memory_mib` | `2048` | Memory limit per container. |
| `max_workspaces` | `4` | Limit on running containers, one per chat or account draft. |
| `idle_seconds` | `900` | Minimum time before stopping an idle container. When the limit is full, another chat can take an eligible slot immediately. |
| `command_seconds` | `900` | Runtime limit for commands started through the tool API. Interactive shells use their terminal sessions. |
| `volume_driver` and `volume_options` | `local` and `{}` | Docker volume driver and JSON object of string options for the user volume. Configure a quota-capable volume driver to enforce a hard disk limit. |

The image includes Bash, Python, pytest, C/C++ tools, CMake, Ninja, Sphinx and
sudo. Containers are CPU-only. Commands run as root inside the container with
only the capabilities that `apt`, `dpkg` and `sudo` need, and set-user-ID
programs grant no further privileges. Only the provisioner has the Docker
socket. Each container has a writable root filesystem, a bounded temporary
filesystem at `/tmp`, its account's folder of the user volume at `/home/user`
and an unpublished API on its own internal bridge. An idle container stops
and gives up its bridge. Its next request starts it again with its files and
packages. Volume and container storage follow the backing filesystem unless
the operator configures quotas.
[Docker volume lifecycle](https://docs.docker.com/engine/storage/volumes/) · [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)

Verification and schema discovery create no containers. For workspace
requests, Open WebUI supplies its authenticated user ID and chat context. The
provisioner authenticates HTTP requests and terminal WebSockets and routes
both to the container of the account and chat. Idle cleanup preserves active
jobs and streams and stops only idle containers. Deleting a saved chat also
closes its container's streams. [HTTP proxy](https://github.com/open-webui/open-webui/blob/v0.13.4/backend/open_webui/routers/terminals.py#L284) · [WebSocket proxy](https://github.com/open-webui/open-webui/blob/v0.13.4/backend/open_webui/routers/terminals.py#L603)

### Network access

With `network = isolated`, a container reaches only the provisioner over its
own internal bridge. The bridge uses IPv4 with no default route, and IPv6 is
disabled. `apt` can then install only from a local repository.
[Install packages in a chat](#install-packages-in-a-chat)

With `network = outbound`, each container also joins a shared bridge with
outbound access, on which containers cannot reach each other. They reach
every address the Docker host reaches: an APT mirror, the internet, other
hosts on its network and the host's published ports, including Open WebUI.
Use it only where that access is acceptable. Changing the setting replaces
existing chat containers on the next start, which removes their `/home/chat`
folders and installed packages.

## Package mirror for image builds

Set `apt_source_file` to a Debian `.list` or `.sources` file. Relative paths
start at the project directory. During the build, the file replaces the
inherited APT sources. The Docker builder must be able to reach the configured
mirrors, and their suites must match the base image. A blank value keeps the
base image's APT sources. The file is stored in the finished image, so use
mirror URLs without embedded credentials.
[Debian APT source syntax](https://manpages.debian.org/bookworm/apt/sources.list.5.en.html)

```ini
[terminal_server]
apt_source_file = config/offline-mirror.list
```

```text
deb http://apt-mirror.lan/debian trixie main
```

Run `loaf build` while the mirror is reachable, then transfer the image to the
offline host. The installed commands run inside the isolated workspaces.
Packages installed during the build are available to all chats.

## Edit an APT source on the offline host

Copy a readable `.list` or `.sources` file to the Docker host and set its
path in the project configuration. For example, place the file at
`config/offline-mirror.sources` after restoring the project.

```ini
[terminal_server]
apt_source_file = config/offline-mirror.sources
```

Run `loaf offline` using the updated launcher and the loaded Open Terminal
image. Loaf mounts the file read-only at
`/etc/apt/sources.list.d/loaf-terminal.sources` in the provisioner and its
chat containers. A `.list` file gets the corresponding `.list` destination.
The file is visible to terminal users. Keep it free of mirror credentials.
An image built without `apt_source_file` keeps its inherited sources as well.
`apt-get update` then reports the unreachable ones and continues with the
configured source.

Edit the source on the host. In-place writes are visible through the mount.
Run `loaf offline` again after saving through an editor that replaces the
file. On that run, Loaf recreates the terminal server if the source changed.
The terminal server then replaces existing chat containers, which removes
their `/home/chat` folders and installed packages.
[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

## Install packages in a chat

Commands run as root, so `apt`, `apt-get` and `sudo apt-get` work as usual in
a chat's terminal and in model commands:

```text
apt-get update
apt-get install -y PACKAGE
```

The packages stay in the chat's container while the chat exists, also while
the container is stopped. Other chats and new chats start from the shared
terminal image. Backups do not include installed packages; install them again
after a restore. Changing `apt_source_file`, `apt_repository_dir` or
`network` replaces existing chat containers on the next start, without their
packages.

The container must reach a package source. With `network = outbound`, it
uses the image's APT sources or a configured `apt_source_file` over the
Docker host's network. With `network = isolated`, set `apt_repository_dir` to
a repository stored on the Docker host and use a source URI rooted at
`file:/run/loaf-apt/repository`. Relative paths in `config.ini` start at the
project directory. `loaf online`, `loaf offline` and `loaf status` refuse a
directory without an APT index: `Packages` or one of its compressed forms,
`Release` or `InRelease` at the root, or `dists/`. For example, after loading
the terminal image, use the following settings and a flat repository's
`.list` source.

```ini
[terminal_server]
apt_source_file = config/offline-mirror.list
apt_repository_dir = /media/transfer/apt-mirror
```

```text
deb [trusted=yes] file:/run/loaf-apt/repository ./
```

To try this, run `loaf setup --test-local-repository` while connected after
building the terminal image. It downloads three command-line tools, chosen at
random from a fixed list, together with the dependencies that the terminal
image lacks, into `apt-test-repository/`. It writes the source file above as
`config/apt-test-repository.list` and prints the tools and the two settings.
Then ask the model to install one of the tools.

APT refuses an unsigned repository unless the source marks it trusted, as
above. A flat repository built with `dpkg-scanpackages` has no signature.
To verify a signed repository instead, place its public key in the repository
directory and reference it with
`signed-by=/run/loaf-apt/repository/KEY.gpg`. The repository is mounted
read-only in every chat container.
[Debian APT source options](https://manpages.debian.org/bookworm/apt/sources.list.5.en.html)

## Offline transfer and shutdown

`loaf backup` includes the matching Open Terminal image when capturing images
if it exists locally.
Use `loaf backup --no-terminal-server` to skip that image. The `terminal`
type transfers the user volume with every account's `/home/user` folder and
the bearer key, together with the `webui` data that owns those accounts. Chat
folders and installed packages stay behind. Name it, as in
`loaf backup terminal`, or set `[terminal_server] include_in_backup = true`
to include it in backups without type names. The archive contains the bearer
key in plaintext and is created with owner-only permissions.
[Archive types](transfer.md#select-archive-types)

`loaf restore` restores those assets. Restore with the matching deployment
version so the volume name and WebUI account IDs match. The destination's
`volume_driver` and `volume_options` determine how the user volume is
created. Restore writes the key to `secret_file` with owner-only permissions,
reuses a matching key, and refuses to replace a different key or overwrite a
populated user volume. In the system deployment, the key stays at
`/etc/loaf/terminal-server.key`, readable by root and the `docker` group,
which runs Compose. Setup from the copied backup uses its saved key when
creating the local file.

For archives without the terminal image, save and load it separately before
serving. Use the matching image tag for a versioned deployment. If you also
need to transfer the bearer key separately, copy it to `secret_file`.

```bash
docker image save -o open-terminal.tar local/open-terminal:latest
docker image load -i open-terminal.tar
```

To move the user volume separately, stop the provisioner and preserve its
name, ownership labels, and data. Restore the matching Open WebUI data so the
account IDs still match the folders, which are named by a hash of the
account ID. Find the user volume by Compose project.

```bash
docker volume ls --filter label=loaf.terminal.users --filter label=loaf.tool-server.project=local-inference
```

`loaf stop` stops the provisioner, which stops the chat containers and keeps
them, and removes the Compose service containers. Use `loaf stop -n` to keep
the stopped service containers and their logs. To disable the extension, set
`enabled = false` and run the desired serving command again. Disable the
saved connection in Open WebUI to hide it from the terminal menu. The user
volume and the stopped chat containers remain available for re-enabling.
[Shutdown options](launcher.md#stop-and-remove-containers)

## Acceptance checks

Build `local/open-webui:latest` and `local/open-terminal:latest` while
connected. Run the checks on native Debian and on Debian in WSL2. Repeat
on each host after disconnecting its internet access once the images and
test dependencies are available locally.

```bash
RUN_TERMINAL_SERVER_TESTS=1 loaf test -v
```

The serving checks use production isolation by default, including addressless internal
bridges on Engine 26 and 27. They use the local daemon and `ip` from
`iproute2` to check the host bridge addresses. On Engine 26 and 27,
`[tests] allow_legacy_terminal_bridge = true` or
`ALLOW_LEGACY_TERMINAL_BRIDGE=1` runs the fixture with ordinary internal
bridges for comparison. Those bridges retain a host address. The override
applies only to tests.
[Docker bridge gateway modes](https://docs.docker.com/engine/network/port-publishing/#gateway-modes)

The online and offline checks run concurrently in separate disposable Compose projects.
Each verifies the current Open WebUI connection and exercises a saved connection
with the old URL. It makes terminal and file requests for two signed-in users
and rejects a third user.
Both phases run on internal networks with image pulls disabled. For the
online service, the test removes a second network attachment before the cold
restart. Both services restart using their saved Open WebUI data and the same
user volume. The checks cover the default and recent entries in the
working-directory menu and a chat that starts in its own folder. They verify
that the Files API lists files created by commands, that other chats see the
shared `user` files and have their own `chat` files, and that chat deletion
removes the chat's container. An archived chat retains its files across the
restart. A separate check installs a package with `apt` and `sudo apt-get`
from a local repository as root and checks that the saved chat keeps it
across a restart while a temporary chat starts fresh.

- **Software** — Compile a C library and C++ client with GCC and G++ using
  CMake and Ninja. Reproduce a failing check, edit the C source with Python,
  then rebuild and verify it with CTest and pytest. Rebuild again after
  restarting.
- **Documentation** — Upload a reStructuredText page through Open WebUI,
  build it with Sphinx, and download the HTML in a ZIP archive. After
  restarting, verify the original HTML, upload a revision, and rebuild.
- **Isolation and limits** — Verify that both accounts keep their own
  workspace files, the second account cannot download the first account's
  artifact, commands run as root in the selected directory, and a command ends
  when it exceeds the runtime limit. With production isolation, check that the
  host bridges have no IPv4 address, the containers have no default route or
  IPv6 address, and a container cannot reach its peer or the provisioner's
  control API.

These checks need no GPU, Ollama process, or model download. To check model
tool calling and the browser terminal, select the saved connection in a chat
and ask a local model with tool support to run a build and open the terminal
or download an artifact.
