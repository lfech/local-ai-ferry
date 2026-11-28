# Per-user terminal server in Open WebUI

The optional terminal server runs on native Debian and Debian in WSL2 with
Docker Engine **26 or newer** and Compose **2.26.1 or newer**. Compose
2.30 and newer support the direct GPU setting for the serving stack. On Engine 26
and 27, each workspace uses an internal bridge with
`com.docker.network.bridge.inhibit_ipv4=true`. Engine 28 and newer use
Docker's `isolated` gateway mode. Both keep an IPv4 address off the host
bridge.

[Bridge driver options](https://docs.docker.com/engine/network/drivers/bridge/#skip-bridge-ip-address-configuration) ·
[Docker Engine 28 release notes](https://docs.docker.com/engine/release-notes/28/)

## Selected design

The provisioner and every chat container start from the shared local image
built from MIT-licensed Open Terminal. Each chat gets its own container, and
each account one more for drafts before their first message. A container
keeps its `/home/chat` folder and the packages installed with `apt` while its
chat exists. One volume holds every account's `/home/user` folder, and a
container mounts only its own account's folder there. Users select one direct
Open Terminal connection in Open WebUI and use its terminal, files, and model
tools. Ollama continues to handle inference.

[Open Terminal 0.17.20 license](https://github.com/open-webui/open-terminal/blob/v0.17.20/LICENSE) ·
[Open WebUI terminal connection](https://docs.openwebui.com/features/open-terminal/setup/connecting/)

## Enable and connect

Add or update these settings in `config/config.ini`. The checkout's and the
packaged settings leave the service disabled, and loaf ignores the other
`[terminal_server]` settings, such as `network`, until it is enabled. The default base image is
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

`loaf setup terminal-server-key` creates the key at the configured path with
owner-only permissions and keeps an existing key unless `--force` replaces it;
plain setup creates no key. After a replacement, update the key of the saved
terminal connection in Open WebUI's admin settings.
A restore of the `terminal` type brings the backup's key instead. For a new
deployment, run these commands while connected.

```bash
loaf setup
loaf setup terminal-server-key
loaf build
loaf run offline
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
file, start the deployment, and update the stored WebUI connection.

Open WebUI starts with the connection already set up. With the terminal
server enabled, loaf mounts the bearer key in the WebUI container, and its
start script passes the connection to Open WebUI's `TERMINAL_SERVER_CONNECTIONS`
setting: **Terminal** at `http://terminal-server:8000` with **Bearer**
authentication and the key. The key does not appear in the container's
configuration. Open WebUI stores the connection when it has no terminal
connections yet, which is the case when WebUI first starts with the terminal
server enabled. Users select it in the chat terminal menu.

`webui_access` decides who may use the connection: `admins`, the default, or
`users` for every account. To allow selected groups instead, keep `admins`
and add the groups to the connection in the Open WebUI admin
**Settings → Integrations → Open Terminal**.

Afterwards Open WebUI keeps its stored connection, including changes made in
its admin settings, and ignores the preset. After a key rotation, or for a
WebUI that first started without the terminal server, add or update the
connection there: a direct connection to `http://terminal-server:8000` with
**Bearer** authentication and the key. Verify and save it.

[Open WebUI environment configuration](https://docs.openwebui.com/reference/env-configuration) ·
[Connection setup](https://docs.openwebui.com/features/open-terminal/setup/connecting/)

## Use the terminal from a model

In **Workspace → Models**, enable **Terminal** on the model and keep
**Function Calling** at its native default. Select **Open Terminal** in the
chat, or choose it as that model's **Default terminal**. Clear the default
to attach it manually per chat. To disable terminal access for that model,
turn off its **Terminal** capability. The settings belong to that model.
[Open WebUI model settings](https://docs.openwebui.com/features/workspace/models/)

Open WebUI adds the terminal server's instructions to the system prompt of
every chat that uses the terminal. The bundled terminal server answers this
request itself, without a chat container, so the instructions also arrive
with a chat's first message, while its container still starts:

```text
You have a Linux terminal: this chat's own Debian container, where you are root. When the user asks you to run, check, install, build, create, read or change something on the computer, do it yourself with the terminal tools instead of describing it or asking the user to do it: run_command runs shell commands for file work, searches and checks. Use get_process_status for a running command and display_file to show an artifact. Answer from the tool results. Install packages with apt update, then apt install PACKAGE. /home/chat is deleted with the chat; keep lasting files in /home/user. Answer general questions directly.
```

The provisioner adjusts the APT guidance to the workspace's package sources.
When there is no reachable configured source, it tells the model to use the
installed programs. [Network access](#network-access)

### Terminal tools

| Tool | Use |
| --- | --- |
| `run_command` | Run shell commands for builds, tests and file operations. Use `ls` or `find` to list files, `grep` to search, and `sed` or `cat` to read text. |
| `get_process_status` | Read output from a running command by its ID and wait for completion. |
| `display_file` | Present an artifact from the workspace in the chat. |
| `read_user_terminal` | Read the output of the user's shared interactive terminal when Open WebUI makes it available. |
| `send_user_terminal_input` | Send input to that interactive terminal. |

The Files tab, interactive terminal and command streams keep their workspace
APIs. The reduced tool list applies to all models using this connection.

`run_command` waits up to `command_wait_seconds`, 120 by default, for the
command to finish and returns its output and exit code, so the chat shows a
command and its result in one **Ran command** entry. A longer command returns
its output so far with status `running`; the model then reads the rest with
`get_process_status`, which waits as long again and appears as another entry.
The terminal server adds this wait to calls that name none, as Open
Terminal's own `execute_timeout` setting does, and its description of
`run_command` names it. A change applies to existing chat containers with
`loaf restart`. `command_wait_seconds = 0` returns at once with the command's
ID, Open Terminal's default.

Open WebUI v0.13.4 builds the **Ran command** entry from the finished tool
result, so the entry shows a spinner and its output appears when the command
ends. To follow a command while it runs, open the chat's **Files** tab: once
the model has run a command, a **Sessions** strip appears below the file
browser. Open it and select the tab labelled with the command; a green dot
marks a running command, and its stdout and stderr arrive as the command
writes them. Collapsing the strip or dismissing a finished tab does not affect
the command.

If a follow-up after a terminal tool result fails with a role-order error,
enable the optional filter for that model.

Use the **Working Directory** folder button beside the terminal selector
below the chat input. Select `user` or `chat` directly under **Recently Used**.
The bundled terminal supplies both as default choices alongside your recent
folders. Use **Open folder** to browse for another directory. If the terminal
selector is in the **+** menu, **Working Directory** is there too.

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
Use Open Terminal's run_command once to execute this exact shell command, then report its stdout.

dir=$(mktemp -d "$HOME/hello-check.XXXXXX") && cd "$dir" && printf '%s\n' '#include <stdio.h>' 'int main(void) { puts("Hello, World!"); return 0; }' > world.c && gcc -Wall -Wextra world.c -o world && ./world
```

[Help small models call the tools](#help-small-models-call-the-tools) ·
[Open WebUI terminal pane](https://docs.openwebui.com/features/open-terminal/terminal-pane) ·
[Open WebUI model settings](https://docs.openwebui.com/features/workspace/models/) ·
[Open Terminal execute timeout](https://docs.openwebui.com/features/open-terminal/advanced/configuration) ·
[Conversation roles must alternate](troubleshooting.md#conversation-roles-must-alternate) ·
[Open WebUI working directory picker](https://docs.openwebui.com/features/open-terminal/file-browser/#working-directory) ·
[Open WebUI file browser root](https://docs.openwebui.com/features/open-terminal/terminals/orchestration/file-browser-home-boundary/)

### Help small models call the tools

Small models such as Ministral 3B or Qwen2.5 7B often describe a command
instead of calling a tool, or stop after a call that returned no output. The
bundled terminal server addresses both, and the model settings in Open WebUI
do the rest:

1. **Instructions with every message.** The system prompt above names the
   requests on which the model acts and the tool for each. Open WebUI waits at
   most three seconds for it; since the terminal server answers without a
   container, a starting container no longer drops it from a chat's first
   message.
2. **One call per command.** With `command_wait_seconds`, `run_command`
   returns the output instead of a command ID, so no second call is needed
   for a usual command.
3. **Shell operations.** The model can use `run_command` for file reads,
   writes, searches and build commands. [Terminal tools](#terminal-tools)
4. **Fewer tool definitions.** Every enabled tool adds a definition to each
   request, and studies of tool selection report that small models choose
   better among fewer tools. In the model's settings, turn off the built-in
   tool categories that the model does not need, such as memory, notes or web
   search, or enable Open WebUI's experimental **Tool Search** for native mode.
5. **A model trained for tool calls.** Prefer instruction-tuned models with
   tool support, such as Qwen2.5 Instruct, Qwen3, Ministral or Granite 4.0.
   In a check, Qwen2.5-Coder 7B wrote its calls as JSON text, which Ollama
   does not turn into tool calls. Use the optional terminal model or an
   existing chat model with tool support. Small models can struggle with
   multi-step tool chains, so give them
   one task per message. Keep `context_length` large enough for the tool
   definitions and the command output. The default is 16384 tokens.
   [Optional terminal model](models.md#opt-in-terminal-model)
6. **Your own instructions.** Open WebUI adds the account's
   `/home/user/AGENTS.md` to every message, so lasting instructions for your
   work belong there. A skill in `/home/user/.agents/skills` needs a
   `view_skill` call before it helps, which small models rarely make, so the
   basic instructions are in the system prompt instead.

A previous check with the full tool list and eight terminal requests, without
Open WebUI in between, measured the first call: Qwen2.5 7B Instruct and Qwen3
4B called a tool for every request with or without the system prompt.
Ministral 3B Reasoning called one for 20 of 24 requests without it and for
all 24 with it, and Granite 4.0 H 350M for 14 and 17 of 24. The
[verification record](verification-record.md) has the details.

[Open WebUI tool calling](https://docs.openwebui.com/features/extensibility/plugin/tools/) ·
[Open Terminal model requirements](https://docs.openwebui.com/features/open-terminal/) ·
[Fewer tools for small models](https://arxiv.org/abs/2411.15399) ·
[Function calling with small language models](https://techcommunity.microsoft.com/blog/educatordeveloperblog/function-calling-with-small-language-models/4472720)

## Install packages in a chat

Commands run as root, so `apt`, `apt-get` and `sudo apt-get` work as usual in
a chat's terminal and in model commands. A new chat container has no package
lists yet, so update them once before the first install:

```text
apt update
apt install PACKAGE
```

Model commands run without a terminal. The terminal image therefore sets
`APT::Get::Assume-Yes` and `Dpkg::Use-Pty "0"`: `apt install` proceeds
without `-y` and writes plain dpkg output without progress bars. This
applies to the interactive terminal as well. The terminal image needs a
rebuild with `loaf build` for these settings. The terminal server's system
prompt names the update step.

The packages stay in the chat's container while the chat exists, also while
the container is stopped. Other chats and new chats start from the shared
terminal image. Backups do not include installed packages; install them again
after a restore. Moving `apt_source_file` or `apt_repository_dir` to another
path, switching between `.list` and `.sources`, or switching network modes
replaces existing chat containers and their packages on the next start.

Set `apt_repository_dir` to a repository on the Docker host. Loaf generates
the binary `deb` entries for `file:/run/loaf-apt/repository` that an enabled
binary source in `apt_source_file` has yet to cover. Relative paths in
`config.ini` start at the project directory. Loaf requires an APT index, such as `Packages` or one
of its compressed forms for a flat repository, or a suite under `dists/`.
The following example uses a flat repository as the only source.
See which sources each mode keeps enabled under [Network access](#network-access).

```ini
[terminal_server]
apt_source_file = config/offline-mirror.list
apt_repository_dir = /media/transfer/apt-mirror
```

```text
deb [trusted=yes] file:/run/loaf-apt/repository ./
```

To try this, run `loaf setup create-file-repository` while connected after
building the terminal image. Then ask the
model to install one of the tools that it prints. The repository directory is
not part of any backup type.

APT refuses an unsigned repository unless the source marks it trusted, as
above. A flat repository built with `dpkg-scanpackages` has no signature.
To verify a signed repository instead, place its public key in the repository
directory and reference it with
`signed-by=/run/loaf-apt/repository/KEY.gpg`. The repository is mounted
read-only in every chat container.

[Model instructions](#use-the-terminal-from-a-model) · [What it creates](cli-help.md#setup) ·
[Debian APT source options](https://manpages.debian.org/bookworm/apt/sources.list.5.en.html)

## Workspace policy

All settings below belong to `[terminal_server]` in `config/config.ini`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `enabled` | `false` | Start the provisioner with `loaf run online` and `loaf run offline`. |
| `include_in_backup` | `true` | Include the `terminal` type in `loaf backup` without type names while the terminal server is enabled: the user volume and the bearer key that Open WebUI's saved connection uses. |
| `secret_file` | `config/terminal-server.key` | Source for the Compose bearer secret. |
| `apt_source_file` | blank | Replace the terminal image's APT sources in chat containers. [Apply changes to chat containers](#edit-an-apt-source-on-the-offline-host) |
| `apt_repository_dir` | blank | Host directory for a `file:` repository, mounted read-only in chat containers. [Install packages in a chat](#install-packages-in-a-chat) |
| `network` | `internet` | Choose `isolated`, `host`, `local` or `internet`. [Network access](#network-access) |
| `webui_access` | `admins` | Accounts that may use the connection that Open WebUI starts with: `admins`, or `users` for every account. [Enable and connect](#enable-and-connect) |
| `cpus` | `2` | CPU limit per container. |
| `memory_mib` | `2048` | Memory limit per container. |
| `max_workspaces` | `4` | Limit on running containers, one per chat or account draft. |
| `idle_seconds` | `900` | Minimum time before stopping an idle container. When the limit is full, another chat can take an eligible slot immediately. |
| `command_seconds` | `900` | Runtime limit for commands started through the tool API. Interactive shells use their terminal sessions. |
| `command_wait_seconds` | `120` | Time from 0 to 240 seconds that `run_command` and `get_process_status` wait for a command when the model names no wait, so that its output comes in the same tool call. `0` returns at once. [Use the terminal from a model](#use-the-terminal-from-a-model) |
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

Verification, schema discovery and the system prompt create no containers. For workspace
requests, Open WebUI supplies its authenticated user ID and chat context. The
provisioner authenticates HTTP requests and terminal WebSockets and routes
both to the container of the account and chat. Idle cleanup preserves active
jobs and streams and stops only idle containers. Open Terminal keeps a shell
running after its browser tab disconnects, which would keep the container
busy. Once a container has had no request or attached terminal stream for
`idle_seconds`, the provisioner closes the account's remaining shells in it
through Open Terminal's `/api/terminals`, then stops the container if no
other process runs. A container recovered after a provisioner restart keeps
its shells until the account uses it again. Deleting a saved chat also
closes its container's streams.

[Docker volume lifecycle](https://docs.docker.com/engine/storage/volumes/) ·
[Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/) ·
[HTTP proxy](https://github.com/open-webui/open-webui/blob/v0.13.4/backend/open_webui/routers/terminals.py#L284) ·
[WebSocket proxy](https://github.com/open-webui/open-webui/blob/v0.13.4/backend/open_webui/routers/terminals.py#L603)

### Network access

| Mode | Container access | APT sources |
| --- | --- | --- |
| `internet` (default) | The provisioner and every address the Docker host can reach. | The image's sources or `apt_source_file`, plus any configured `apt_repository_dir`. |
| `local` | The provisioner and every address the Docker host can reach, including the internet when available. | The configured `apt_source_file` and `apt_repository_dir`. The image's default Debian sources are disabled. |
| `host` | The provisioner and services listening on the Docker host's bridge address at `host.docker.internal`. The chat has no default route. | An explicit HTTP(S) mirror at `host.docker.internal` or a mounted file repository. |
| `isolated` | The provisioner over the chat's internal bridge with no default route. | A file repository mounted with `apt_repository_dir`. |

Both outbound modes use the shared `loaf-terminal-connected` bridge. Chat
containers on it cannot reach each other. `host` uses a separate internal
bridge with a host IPv4 address and direct chat-to-chat traffic disabled.
In `local`, set only reachable LAN or file sources to avoid attempts against
internet mirrors.
Loaf validates UTF-8 `.list` and `.sources` syntax with the host's `apt-get`
without downloading indexes. In `host` and `isolated`, it checks the configured
APT addresses against the network mode. Apply a mode change with `loaf restart`.
[Docker internal networks](https://docs.docker.com/reference/cli/docker/network/create/#network-internal-mode---internal) ·
[Restart options](cli-help.md#restart)

[Install packages in a chat](#install-packages-in-a-chat)

#### Docker host mirror

Set `network = host` and `apt_source_file = config/host-mirror.list` in
`[terminal_server]`. Use `host.docker.internal` in the source file.

```text
deb http://host.docker.internal:8081/debian trixie main
```

The provisioner creates the bridge when it starts. On the Docker host, find
its IPv4 address and bind the mirror there on the port in the source file.
The example below uses the default deployment namespace.

```bash
docker network inspect loaf-terminal-host --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
```

## Package mirror for image builds

Use `loaf build --with-apt-repos` to build the terminal image with the
configured APT settings. An `apt_source_file` replaces the inherited sources
during the build, and `apt_repository_dir` adds a local repository. The
Docker builder must be able to reach any configured mirrors, and their
suites must match the base image. The finished image keeps the base image's
APT sources. Verbose launcher output shows the source path and omits the
contents passed to BuildKit.

```ini
[terminal_server]
apt_source_file = config/offline-mirror.list
```

```text
deb http://apt-mirror.lan/debian trixie main
```

Run the build while the mirror is reachable, then transfer the image to the
offline host. The installed commands run inside the isolated workspaces.
Packages installed during the build are available to all chats.

[Debian APT source syntax](https://manpages.debian.org/bookworm/apt/sources.list.5.en.html)

## Edit an APT source on the offline host

Copy a readable `.list` or `.sources` file to the Docker host and set its
path in the project configuration. For example, place the file at
`config/offline-mirror.sources` after restoring the project.

```ini
[terminal_server]
apt_source_file = config/offline-mirror.sources
```

Run `loaf restart` after changing `apt_source_file`, `apt_repository_dir` or
the source file's contents. New chat containers use the current settings
with the loaded Open Terminal image. Run `loaf run offline` or `loaf run online`
if the deployment is stopped. [Restart options](cli-help.md#restart)

Loaf mounts the configured file read-only at
`/etc/apt/sources.list.d/apt_source_file.sources` or
`/etc/apt/sources.list.d/apt_source_file.list`. Keep mirror credentials out
of this file because terminal users can read it. The file replaces the
image's APT sources, and Loaf adds the local repository's generated source
in `apt_repository_dir.list` when configured.

An existing chat keeps its container, `/home/chat` folder and installed
packages when only the source file's contents change. The container binds
the updated file the next time it starts. [Mount path and format changes](#install-packages-in-a-chat)

[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

## Offline transfer and shutdown

Backups carry the Open Terminal image, the user volume with every account's
`/home/user` folder and the bearer key, as the
[archive types](transfer.md#select-archive-types) describe; chat folders and
installed packages stay behind. After a restore of a backup made with
`--no-terminal-server`, enable the server again as described in
[Enable and connect](#enable-and-connect). Restore into the matching namespace,
so that the volume name and the WebUI account IDs match; the destination's
`volume_driver` and `volume_options` decide how the user volume is created.

For archives without the terminal image, save and load it separately before
serving. Use the matching image tag for a separate namespace. If you also
need to transfer the bearer key separately, copy it to `secret_file`.

```bash
docker image save -o open-terminal.tar loaf/open-terminal:latest
docker image load -i open-terminal.tar
```

To move the user volume separately, stop the provisioner and preserve its
name, ownership labels, and data. Restore the matching Open WebUI data so the
account IDs still match the folders, which are named by a hash of the
account ID. The user volume is `loaf-terminal-users`, or
`loaf-NAME-terminal-users` in the namespace `loaf-NAME`. The chat containers
and their networks are named `loaf-terminal-KEY`. `loaf docker ps` lists them.

```bash
docker volume inspect --format '{{json .Labels}}' loaf-terminal-users
```

[`loaf stop`](cli-help.md#stop) stops the provisioner, which stops the chat
containers and keeps them. To disable the extension, set
`enabled = false` and run the desired serving command again. Disable the
saved connection in Open WebUI to hide it from the terminal menu. The user
volume and the stopped chat containers remain available for re-enabling.

[Restore behavior](transfer.md#restore-behavior) · [Docker resources](cli-help.md#docker) ·
[Shutdown options](cli-help.md#stop)

## Acceptance checks

Build `loaf/open-webui:latest` and `loaf/open-terminal:latest` while
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

[Docker bridge gateway modes](https://docs.docker.com/engine/network/port-publishing/#gateway-modes)
