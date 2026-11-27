# Per-user terminal server in Open WebUI

The optional terminal server runs on native Debian and Debian in WSL2 with
Docker Engine **26 or newer** and Compose **2.26.1 or newer**. Compose
2.30 and newer support the direct GPU setting for the serving stack. On Engine 26
and 27, each workspace uses an internal bridge with
`com.docker.network.bridge.inhibit_ipv4=true`. Engine 28 and newer use
Docker's `isolated` gateway mode. Both keep an IPv4 address off the host
bridge. [Bridge driver options](https://docs.docker.com/engine/network/drivers/bridge/#skip-bridge-ip-address-configuration) · [Docker Engine 28 release notes](https://docs.docker.com/engine/release-notes/28/)

## Selected design

The provisioner and new workspaces start from the shared local image built from
MIT-licensed Open Terminal. A chat that installs packages uses its own derived
image. Each authorized Open WebUI account gets a persistent
user volume on first use. Each chat has a workspace with up to two containers
that share its files and packages. The user runner serves your terminal and
the **Files** tab and can write to the account's user volume. The agent
runner serves model tool calls and mounts that volume read-only. Users select
one direct Open Terminal connection in Open WebUI and use its terminal, files,
and model tools. Ollama continues to handle inference. [Open Terminal 0.17.20 license](https://github.com/open-webui/open-terminal/blob/v0.17.20/LICENSE) · [Open WebUI terminal connection](https://docs.openwebui.com/features/open-terminal/setup/connecting/)

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

You can choose `chat` before sending the first message. The draft shows an
empty, read-only directory. The provisioner attaches writable chat storage
when the chat starts using the terminal. Upload files to `/home/chat` from
the saved chat's **Files** tab or write them from the terminal. The **home**
breadcrumb in Files lets you browse both folders.

| Folder | Path | You | Model | Lifetime |
| --- | --- | --- | --- | --- |
| `user` | `/home/user` | Read and write | Read only | Shared by your chats. Existing files and settings stay at this path. |
| `chat` | `/home/chat` | Read and write | Read and write | Private to this chat. A saved chat's files survive restarts and are removed when the chat is deleted. A temporary chat's files are removed once it has been idle for `idle_seconds` without a runner. |

A chat starts in `/home/chat`, and a draft in `/home/user`. The Files tab, your
uploads and model commands share the working directory you select. Use
`/home/chat` for files to remove with the chat and `/home/user` for files to keep.
Model commands can read `/home/user` but fail with `Read-only file system`
when they write to it. Copy a model's result into `/home/user` yourself from
the terminal or the Files tab.
[Open WebUI file browser root](https://docs.openwebui.com/features/open-terminal/terminals/orchestration/file-browser-home-boundary/)

Open WebUI calls model tools at the paths that the provisioner publishes in its
tool schema, all below `/agent/`. The provisioner sends those calls to the
agent runner. The interface uses Open Terminal's plain paths, which reach the
user runner. A model therefore never reaches the writable user volume. Its
tools also omit typing into your interactive shell; reading its output remains.
Commands that you start from the interface, such as a Git clone, run in the user
runner. The process list shows the model's commands. Open WebUI without Redis,
as bundled, reloads the tool schema for each request.

The provisioner uses the bundled Open WebUI deployment's SQLite database
to check saved chat ownership and deletion. It reads chat IDs and owners
through a read-only mount. Deleting a chat stops its runner and removes its
volume on the next cleanup pass while the provisioner is running, normally
within about five seconds.
Archiving a chat keeps its folder. A custom deployment using another
database needs a compatible chat lookup for saved chat workspaces.

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
| `include_in_backup` | `false` | Include the `chats` type in `loaf backup` without type names: account and chat folders, chat package images and the bearer key. |
| `secret_file` | `config/terminal-server.key` | Source for the Compose bearer secret. |
| `apt_source_file` | blank | Use this APT source for terminal image builds and per-chat package installs. [Package mirror](#package-mirror-for-image-builds) |
| `apt_repository_dir` | blank | Host directory for a `file:` repository mounted in the package installer. [Chat packages](#install-packages-for-a-chat) |
| `cpus` | `2` | CPU limit per runner. |
| `memory_mib` | `2048` | Memory limit per runner. |
| `max_workspaces` | `4` | Limit on simultaneously allocated workspaces, one per account draft or chat. A workspace uses up to two runners, each with the CPU and memory limits below. |
| `idle_seconds` | `900` | Minimum time before reclaiming an idle runner. When the limit is full, another workspace can take an eligible slot immediately. |
| `command_seconds` | `900` | Runtime limit for commands started through the tool API. Interactive shells use their terminal sessions. |
| `volume_driver` and `volume_options` | `local` and `{}` | Docker volume driver and JSON object of string options. Configure a quota-capable volume driver to enforce a hard workspace disk limit. |

The image includes Bash, Python, pytest, C/C++ tools, CMake, Ninja, and Sphinx.
Runners are CPU-only and unprivileged. The provisioner alone has the Docker
socket. Each runner uses a separate isolated network, an unpublished API, a
read-only root filesystem, a bounded temporary filesystem at `/tmp`, and
storage for the account and current chat. Runner networks use IPv4 with no default
route, and IPv6 is disabled. Named volume capacity follows the backing
filesystem unless the operator configures quotas.
[Docker volume lifecycle](https://docs.docker.com/engine/storage/volumes/) · [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)

Verification and schema discovery create no workspaces. For workspace
requests, Open WebUI supplies its authenticated user ID and chat context. The
provisioner authenticates HTTP requests and terminal WebSockets and routes
both to the runner scoped to the account and chat. Idle cleanup preserves
active jobs and streams and leaves named volumes in place. Deleting a
saved chat also closes its runner and streams. [HTTP proxy](https://github.com/open-webui/open-webui/blob/v0.13.4/backend/open_webui/routers/terminals.py#L284) · [WebSocket proxy](https://github.com/open-webui/open-webui/blob/v0.13.4/backend/open_webui/routers/terminals.py#L603)

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
workspaces. A `.list` file gets the corresponding `.list` destination.
The file is visible to workspace users. Keep it free of mirror credentials.

Edit the source on the host. In-place writes are visible through the mount.
Run `loaf offline` again after saving through an editor that replaces the
file. On that run, Loaf recreates the terminal containers if the source changed.
The workspace root filesystem is read-only. Chat package installs use the
same configured source through a separate installer.
[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

## Install packages for a chat

With `apt_source_file` set, `loaf offline` and `loaf online` let the model
change the APT packages of the current chat. Ask the model to install a
package. It can use the `install_chat_packages` and `remove_chat_packages`
tools or run a plain APT command, which the provisioner handles in place of
the runner:

```text
sudo apt-get update && sudo apt-get install -y PACKAGE
apt-get remove PACKAGE
apt-get purge PACKAGE
```

The provisioner accepts `apt` and `apt-get` with `install`, `reinstall`,
`remove` and `purge`, the options `-y`, `-q` and `--no-install-recommends`,
and optional `sudo` and environment assignments, joined by `&&` or `;`.
`update`, `autoremove` and `clean` need no action. Each package name can
carry an architecture or version, such as `jq:amd64` or `jq=1.7.1-3`. Any
other command, such as one with a pipe or another program, runs unchanged in
the runner. There the image's `apt` and `apt-get` explain how to change
packages and exit with status 100. An image built before this release lacks
these explanations; run `loaf build` to add them.

Each chat's packages form a list. Every change installs the whole list
with APT from the configured source, in a disposable installer based on the
shared terminal image. The result becomes the chat's image, and removing the
last package removes the image. Recommended packages follow the image's APT
settings; the provisioner accepts `--no-install-recommends` but does not
change them.
Only packages installed for the chat can be removed; the shared image's
packages stay. The tool result lists the chat's packages. After a change,
the next request starts new runners from the chat's image. Both folders keep
their files and the selected working directory stays. Every new chat starts with the
shared image's packages. Chat images from earlier releases did not record
their packages; their next change rebuilds them with only the new list, and
the result says so.

For an HTTP or HTTPS source, make the mirror reachable from the Docker
host's bridge network. For a mirror stored on the offline host, set
`apt_repository_dir` to the repository root and use a source URI rooted at
`file:/run/loaf-apt/repository`. Relative paths in `config.ini` start at the
project directory. `loaf online`, `loaf offline` and `loaf status` refuse a
directory without an APT index: `Packages` or one of its compressed forms,
`Release` or `InRelease` at the root, or `dists/`. For example, after loading
the base terminal image, use the following settings and a flat repository's
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
`signed-by=/run/loaf-apt/repository/KEY.gpg`. The `file:` repository is
mounted read-only in a network-disabled installer.
[Debian APT source options](https://manpages.debian.org/bookworm/apt/sources.list.5.en.html)

Saved chat images persist on this Docker host across `loaf stop` and are
removed when their chat is deleted. A temporary chat's image is removed after
it has been idle for `idle_seconds` and at provisioner shutdown. The `images`
type of a backup carries the shared terminal base image, and the `chats` type
the saved chats' images.

## Offline transfer and shutdown

`loaf backup` includes the matching Open Terminal image when capturing images
if it exists locally.
Use `loaf backup --no-terminal-server` to skip that image. The `chats` type
transfers each account's `user` volume, each saved chat's `chat` volume, the
saved chats' package images and the bearer key, together with the `webui`
data that owns those accounts and chats. Name it, as in `loaf backup chats`,
or set `[terminal_server] include_in_backup = true` to include it in backups
without type names. Temporary chats stay behind. The archive contains the
bearer key in plaintext and is created with owner-only permissions.
[Archive types](transfer.md#select-archive-types)

`loaf restore` restores those assets. Restore with the matching deployment
version so the workspace names and WebUI account IDs match. The destination's `volume_driver` and `volume_options` determine
how workspace volumes are created. Restore writes the key to `secret_file`
with owner-only permissions, reuses a matching key, and refuses to replace a
different key or overwrite populated volumes. Setup from the copied backup
uses its saved key when creating the local file.

For archives without the terminal image, save and load it separately before
serving. Use the matching image tag for a versioned deployment. If you also
need to transfer the bearer key separately, copy it to `secret_file`.

```bash
docker image save -o open-terminal.tar local/open-terminal:latest
docker image load -i open-terminal.tar
```

To move user volumes separately, stop the provisioner and preserve their
names, ownership labels, and data. Restore the matching Open WebUI data so
account IDs still match the volumes. List user volumes by Compose project.

```bash
docker volume ls --filter label=loaf.tool-server.project=local-inference
```

`loaf stop` stops the provisioner and its runners and removes the Compose
service containers. Use `loaf stop -n` to keep the stopped service containers
and their logs. To disable the extension, set `enabled = false` and run the
desired serving command again. Disable the saved connection in Open WebUI to
hide it from the terminal menu. Workspace volumes remain available for
re-enabling. [Shutdown options](launcher.md#stop-and-remove-containers)

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
workspace volumes. The checks cover the default and recent entries in the
working-directory menu and the transition from a draft to a writable chat
folder. They verify that the Files API lists model-created files, that other chats see the shared
`user` files and have their own `chat` files, and that chat deletion removes
the named chat volume.
An archived chat retains its files across the restart.

- **Software** — Compile a C library and C++ client with GCC and G++ using
  CMake and Ninja. Reproduce a failing check, edit the C source with Python,
  then rebuild and verify it with CTest and pytest. Rebuild again after
  restarting.
- **Documentation** — Upload a reStructuredText page through Open WebUI,
  build it with Sphinx, and download the HTML in a ZIP archive. After
  restarting, verify the original HTML, upload a revision, and rebuild.
- **Isolation and limits** — Verify that both accounts keep their own
  workspace files, the second account cannot download the first account's
  artifact, a model command reads but cannot write the account's folder in the
  selected directory, and a command ends when it exceeds the runtime limit. With
  production isolation, check that the host bridges have no IPv4 address,
  the runners have no default route or IPv6 address, and a runner cannot
  reach its peer or the provisioner's control API.

These checks need no GPU, Ollama process, or model download. To check model
tool calling and the browser terminal, select the saved connection in a chat
and ask a local model with tool support to run a build and open the terminal
or download an artifact.
