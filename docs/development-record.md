# Development record

[Project overview](../README.md) · [Development backlog](instructions/development.md) ·
[Verification record](verification-record.md)

Git milestones use commit timestamps. Working-tree milestones use the
associated verification time where it was recorded.
Earlier entries use the current names for commands, paths and artifacts.

## 2026-11-27 13:37 CET — One terminal container per chat with root, apt and a shared user volume

The author's decisions D1 to D5 replace the terminal design.
[Selected design](terminal-server.md#selected-design)

- **Containers.** Each chat gets one container, and each account one for
  drafts. The account's requests and the model's tools reach the same
  container, and the provisioner publishes Open Terminal's tools at their
  own paths. Removed: the agent runner, the `/agent/` schema, the
  interception of APT commands, the package tools, the per-chat package
  images and the image's `apt` guard.
- **D1, storage.** One volume per deployment holds every account's
  `/home/user` folder. A container mounts only its account's folder as a
  volume subpath. Docker refuses a missing subpath, so a short-lived
  container creates it on the account's first use. `/home/chat` lives in the
  container with its installed packages. `HOME` stays `/home/user`, and a
  chat starts in `/home/chat`.
- **Container lifecycle.**
  - An idle container stops and gives up its bridge; the next request
    reconnects and starts it again.
  - A deleted chat loses its container.
  - Temporary chats lose theirs when idle and at provisioner start and
    shutdown.
  - A container records its runtime mounts, local repository and network
    setting, and a change replaces it.
- **D2, root.** Commands run as root with the capabilities that `apt`,
  `dpkg` and `sudo` need, `no-new-privileges` and a writable root
  filesystem. The image adds `sudo` and keeps `DEBIAN_FRONTEND` through it.
  The author plans a command allowlist and a non-root user later.
- **D3, network.** `[terminal_server] network` selects the access:
  - `isolated`, the default, keeps a container on its internal bridge;
  - `outbound` adds a shared bridge with outbound access and
    inter-container traffic disabled.

  A local `apt_repository_dir` is mounted read-only in every container.
- **D4, backup.** The `chats` type becomes `terminal`: the user volume and
  the bearer key, without chat folders or packages. The archive format is 3,
  and `backup_version_min` accepts only 3.
- **D5, no migration.** Earlier account volumes, chat volumes and chat
  images are ignored; the upgrade note lists them.
- **Fixes found while testing with Docker:**
  - The `local` log driver refuses `max-file=1` with compression, which
    broke the folder helper.
  - The provisioner now logs a Docker error's request and status.
- **Open.** `outbound` gives chat containers every address the Docker host
  reaches, including its published ports. A proxy limited to the mirror
  hosts was proposed but not built.

## 2026-11-27 12:48 CET — System restores share settings with the group; the key keeps its own rule

- Resolves the open point of the previous entry. In the system deployment,
  restored settings get mode 0640 and the group of `/etc/loaf`, and
  installers get the group of `/var/lib/loaf`, the `docker` group from the
  package's postinst. Missing directories are created 0750 with that group.
  The group comes from the deployment directory, not from setgid
  inheritance, so this also works with an older package's 0750
  `/etc/loaf`. Outside the system deployment, restored files stay
  owner-only.
- The terminal key never takes its mode from the directory. One helper,
  `setup.protect_terminal_key`, serves setup and restore. The key stays
  owner-only in projects. In the system deployment it gets `root:docker`
  0640, because the group's members run Compose, which reads it. Before,
  a system restore wrote it owner-only.
- Restoring `config` into the system deployment keeps `[terminal_server]
  secret_file` at the system key path. Before, a portable `config.ini`
  without that line moved the key to `/var/lib/loaf/config/`.
- A restored settings file at the key's path is refused for every restore;
  before, only restores with `chats` refused it.

## 2026-11-27 12:00 CET — Next-step hints for backup folders and a `--system` hint

- The author decided that loaf never selects the system deployment by
  itself. With the loaf Debian package installed (`/var/lib/loaf` exists),
  setup and restore in a backup folder still make it the project. They then
  print `sudo <launcher> --system restore --archive <moved backup>`.
- `./loaf` without a command, or any command other than setup and restore,
  in a backup folder prints the typical steps (setup, restore, offline), the
  alternative of running the backup's launcher from a new project
  directory, and `./loaf docs` with `./loaf docs transfer`. It exits with 2.
- Setup from a backup ends with `Next:` and the restore command, followed by
  the docs hint. Every restore ends with `Next:` and the offline command.
  Inside the project they use the short `./loaf` form and name `--version`
  only when the project's settings would select another one. In the system
  deployment the follow-up uses `--system` instead of
  `--project-dir /var/lib/loaf`.
- Fixed a regression from the previous entry: after setup in the folder of a
  versioned backup, a plain `./loaf restore` misses the moved backup,
  because the default settings select unversioned backups. Setup's hint adds
  `--archive` in that case. When restore finds no backup, it names one of
  another deployment version to select with `--archive`.
- Open, found while writing the hint: restoring `config` into the system
  deployment writes `/etc/loaf` files with mode 0600. As root this hides
  `config.ini` from the `docker` group, and without root the write fails,
  because `/etc/loaf` is `root:docker` 0750.

## 2026-11-27 11:12 CET — A backup folder becomes the project, and source restores overwrite

- The author dropped the home project `~/.local-ai-ferry`. loaf uses the
  current directory or `--project-dir`.
- Setup and restore run in a backup folder make it the project. They move
  the archive, its launcher and `SHA256SUMS` unchanged to
  `backup/<archive name>/` and copy the launcher back to the root, all
  under the project lock and before the first other change. The archive
  name keeps the dated folder name, so later restores find the backup even
  in a renamed folder. A failed move puts the files back. Read-only media
  end in an error that suggests copying the folder or running its launcher
  from the project directory. Other commands refuse a backup folder.
  [Debian restore](transfer.md#debian-restore)
- `source` restores write archived files and links over existing ones and
  keep files that the archive lacks. Directories merge. A file where the
  archive has a directory, or the reverse, stops the restore before any
  change, so nothing is written through links. Publishing moves replaced
  files aside and puts them back on failure.
- Open: whether the backup's launcher should select the system deployment
  when the loaf Debian package is installed. The release handoff installs the
  package and then restores with the backup's launcher into a project, so
  that check depends on the decision.

## 2026-11-27 10:05 CET — `loaf package` builds the executable and the Debian package

- `loaf package binary debian` replaces `loaf binary`, without an alias.
  `loaf build` still builds the deployment images. The author chose
  `package` over `export`, which already names the Modelfile refresh of
  `backup --export` and suggests a deployment transfer. The architecture
  command table follows. [Local builds](releases.md#build-and-check-locally)
- `binary` builds `dist/loaf` and `dist/LICENSE.txt` with
  `scripts/build-release.sh --binary-only`, as before. `debian` checks for
  `dpkg-deb`, builds or reuses the launcher wheel in `install-cache/wheels/`
  the way `loaf setup` does, and runs `scripts/build-deb.sh` into `dist/`.
- Name one or both targets. They run in the given order under the project
  lock, and the first failure stops the command. `--dry-run` previews them.
  Source checkout backups still build `dist/loaf` themselves.

## 2026-11-26 20:45 CET — Display names replace long model names, and review findings

- `loaf download` now removes the long `hf.co/...` name after creating an
  entry's `name`, unless another entry selects the same source without a
  name. WebUI lists such a model once, under its readable name.
  `backup --export` exports the selected names; run download after adding a
  `name` to a cached model. [Model selection](launcher.md#select-models)
- Open points from the code review of the uncommitted changes, ordered by
  impact: `restore volumes` must expand `chats` only when the archive holds
  it; damaged archives and an unusable project directory must end in an
  error message; restore must check volume emptiness and labels before its
  first change; `--archive` on the archive inside a backup folder must still
  check `SHA256SUMS`; the test APT repository's source file is runtime-only
  and breaks `loaf build` when set as `apt_source_file`; `source` alone omits
  tracked `config/` files. Minor: the published tool list should use the
  same check as routing, the account's process history is capped at 64, and
  the shell's `apt` guard misses option values such as `-o Key=Value`.

## 2026-11-26 19:15 CET — Typed backup archives, an unchanged backup folder and a test APT repository

- Archive format 2: the `.tar.gz` begins with `loaf.json`, which lists the
  format, the deployment version and one item per type with each member's
  path, size and SHA-256 hash. loaf reads it before any payload. The types
  are `source`, `config`, `installers`, `images`, `models`, `webui` and
  `chats`; `project` and `volumes` remain as aliases. `chats` holds each
  account's `/home/user`, each saved chat's `/home/chat`, the saved chats'
  package images and the bearer key, and implies `webui`.
  [Archive types](transfer.md#select-archive-types)
- The model archive format, `export`, `import` and `--models-only` are gone:
  `loaf backup models` and `loaf restore models [--delete]` replace them.
  `source`, `installers` and `models` restore into another deployment
  version. `setup --all` is gone; `loaf restore` recovers a source checkout.
- A backup folder now holds `loaf`, the archive and `SHA256SUMS`, which
  replaces `backup.json` and `sha256.json`. loaf never changes it: inside one,
  setup and restore use `~/.local-ai-ferry`, and a backup folder is refused as
  `--project-dir`. Standalone setup copies the launcher into the project. The
  in-place layout with `backup/` and `dist/loaf` is gone.
  [Debian restore](transfer.md#debian-restore)
- Breaking change before 1.0.0: restore refuses archives from earlier
  releases; their own launcher restores them. `[transfer] backup_version_min`
  names the oldest accepted format, currently 2.
- `loaf setup --test-local-repository` downloads three tools, chosen at
  random from 13 Debian command-line tools, with the dependencies that the
  terminal image lacks into `apt-test-repository/`. It writes a trusted
  `file:` source and prints the tools and settings.
  [Chat packages](terminal-server.md#install-packages-in-a-chat)
- Decided against a fully transparent `apt` in the terminal shell: the
  runner cannot reach the provisioner by design, and an image change
  restarts the runner during the user's command. The guard stays.
- Open points: every backup, including `models` alone, needs the standalone
  launcher, and a source checkout builds it first. Backup hashes each staged
  member in an extra read before compression. The `release_handoff.py`
  flow was not rerun.

## 2026-11-26 18:40 CET — Read-only user folder for models and chat package changes through APT

- Each chat workspace now has up to two runners from the same image that
  share its folders. The user runner serves the interface and can write
  `/home/user`. The agent runner serves model tool calls, mounts `/home/user`
  read-only and has `/home/chat` as its home. The provisioner publishes the
  model tools below `/agent/` in its tool schema and routes by path. Commands
  started from the interface run in the user runner, and process views follow
  each command. The tool for typing into the account's shell is no longer
  offered. `max_workspaces` counts workspaces, not runners.
  [Folder access](terminal-server.md#use-the-terminal-from-a-model)
- The working directory selected in the interface applies to both runners.
  A new chat starts in `/home/chat`. Temporary chats use a shared volume,
  removed after `idle_seconds` without a runner and at shutdown, instead of a
  per-runner temporary filesystem.
- Chat packages form a list recorded in the chat image's labels. The model
  changes it with `install_chat_packages`, the new `remove_chat_packages`, or
  a plain `apt`/`apt-get` install, remove or purge command, which the
  provisioner handles instead of the runner. Every change rebuilds the chat
  image from the shared image. Only packages installed for the chat can be
  removed. The image's `apt` and `apt-get` refuse changes in the shell and
  explain this route. [Chat packages](terminal-server.md#install-packages-in-a-chat)
- Probable cause of the reported failure with a local repository: the
  working `config/config.ini` leaves `apt_source_file` blank, which disables
  package changes. The guide now shows `[trusted=yes]` for an unsigned
  `file:` repository, and the launcher refuses an `apt_repository_dir`
  without an APT index. The failure itself was not reproduced.
- Approved changes to the [architecture](instructions/architecture.md):
  the agent runner, the read-only user folder and package lists.
- Open points: run `loaf build` so the image contains the shell's `apt`
  guard; earlier images still work through the runtime scripts. Chat images
  from earlier releases lack a package list and are rebuilt with only the
  next change's packages.

## 2026-11-26 12:05 CET — Three installation channels, settings layers and the Debian package

- Upgrades: a `docker-project` link to another loaf release no longer
  supplies Compose files. The launcher uses its own deployment files, and
  `setup` and `link-docker-project` update such links in place. Added
  `loaf version`. [Upgrade the standalone executable](launcher.md#upgrade-the-standalone-executable)
- Settings layers: packaged defaults, then `/etc/loaf`, then the project's
  `config/config.ini`. Only a project can move or skip the system layer
  with `[launcher] system_config`. The model list comes from the highest
  layer with `models.ini`. `status` lists the settings files. `setup` and
  `status` warn when a project and the system deployment share a Docker
  namespace. Standalone setup writes a commented `config.ini` and no
  longer copies `models.ini`. [Settings layers](configuration.md#project-configuration)
- System deployment: `--system` selects `/etc/loaf` and `/var/lib/loaf`,
  and the Debian package selects it by default. A setgid project directory
  shares its lock with the directory's group. The system terminal key is
  readable by root and the `docker` group.
- Guides: every channel bundles the README, guides, architecture and
  examples. `loaf docs` lists and prints them, and `loaf docs loaf.1`
  prints the generated manual page. Standalone setup exports them to a
  stamped `docs/` with links into `docker-project/` or to the release.
  [Bundled guides](launcher.md#read-the-bundled-guides)
- Debian package: `scripts/build-deb.sh` turns the wheel into
  `loaf_<version>_all.deb` for Debian 12 and 13, with a conffile in
  `/etc/loaf`, maintainer scripts, a disabled `loaf.service`, the manual
  page and DEP-5 copyright. The release jobs build, check and attach it,
  and the disconnected handoff installs it. [Debian package](installation.md#debian-package)
- Backups: `project` now holds settings, Modelfiles and installer caches
  for every channel. A checkout's Git history and working files form the
  new `source` component, which only `setup --all` applies. Backups no
  longer carry `.venv`. Checkout backups made before this change have no
  `source` payload and cannot be used with `setup --all` of this version.
  [Component selection](transfer.md#select-archive-types)
- A checkout's setup links `./loaf` to `.venv/bin/loaf`, and the guides use
  `./loaf` throughout. The Verify workflow adds Python 3.11 and the Debian
  package tests.
- Open points: Debian's own NVIDIA drivers, 535 in Debian 12 and 550 in
  Debian 13, are too old for the default CUDA 13 build. Debian 12 needs
  Docker's repository. The package's `Maintainer` field uses a placeholder
  until `LOAF_DEB_MAINTAINER` is set for releases.

## 2026-11-26 10:47 CET — Test packages through setup

- Added `loaf setup tests`. It caches `aiohttp`, `yarl`, `httpx` and Open
  Terminal, pinned to the terminal image in `tests/requirements.txt`, together
  with their requirements as wheels in `install-cache/wheels/`. It then
  installs them from that directory into the virtualenv that runs
  `loaf test`. A separate cache record holds the inputs and wheel digests.
  Repeat and recovered runs install offline. [Test packages](launcher.md#test-packages)
- Backups carry the test wheels and their record. The Python 3.13 Verify job
  uses the command, so the provisioner and runner tests run in CI.
- The command requires Python 3.11 or newer, like Open Terminal, and
  accepts only `--dry-run` and the global flags.

## 2026-11-26 10:20 CET — Review of test coverage and runtime

- Added Docker-free tests for restore input that must be refused:
  volume entries outside the restored volumes, device files and
  cross-volume hard links, payload archives with unexpected members or
  invalid hash manifests, incomplete v1 model manifests, and reserved or
  absolute checkout paths. Each new test fails when its check is removed.
  [Verification coverage](verification.md#verification-coverage)
- Added a table-driven CLI test for 28 rejected option combinations. Each
  exits with status 2 and its specific message before any subprocess.
- Packaged settings must equal the built-in defaults, and the Compose and
  Dockerfile image fallbacks must name the same images.
- The GitHub Verify job on Python 3.13 installs the terminal image's
  `aiohttp` and `yarl` versions so that the provisioner tests run on every
  push. The verification guide shows how to run the provisioner and runner
  tests inside the built terminal image. [Terminal server checks](verification.md#terminal-server-checks)
- The offline and connected terminal workspace checks now run concurrently
  as subtests of one test. Both keep their separate Compose projects.

## 2026-11-26 10:06 CET — Build inputs inside their contexts and compact default images

- Both Ollama images now build from the single `docker/ollama` context that
  holds their Dockerfile, export helper and startup wrapper. Compose before
  5.7.0 lets Buildx read only build contexts. With Buildx 0.37.2 and newer,
  the earlier `../ollama/Dockerfile` reference failed whenever the deployment
  data was outside the working directory, as with the standalone executable
  and wheel installs. A Compose contract test keeps each Dockerfile and
  build secret inside its context. [Build privileges](troubleshooting.md#build-requests-additional-privileges)
- Removed `BUILDX_BAKE_ENTITLEMENTS_FS=0` from the GitHub release job. Its
  handoff now builds the standalone executable's images the same way as an
  operator's host.
- The packaged `[images]` section now matches the checkout's: the bundled
  `loaf-ollama-cuda13` recipe and Open WebUI v0.13.4 slim. The built-in,
  Compose and Dockerfile fallbacks match, and a Docker-free test compares
  them. Documented the NVIDIA driver 580 requirement and the features that
  slim omits. [Ollama build selection](configuration.md#select-an-ollama-build-recipe) ·
  [WebUI image](configuration.md#select-the-webui-image)

## 2026-11-26 00:33 CET — Releases v0.2.0 to v0.2.2

- Set the package version to 0.2.0, 0.2.1 and 0.2.2 in successive commits
  and kept the release notes, titled for v0.2.0, in the annotated `v0.2.2`
  tag. The temporary `commit.md` notes file has been removed.
- `BUILDX_BAKE_ENTITLEMENTS_FS=0` was first set on the upload step, then
  moved to the handoff step for v0.2.2. The v0.2.2 release job then passed
  according to the author. The entry above replaces this workaround.
- The runner image documentation lists Compose 2.38.2 and Buildx 0.39.0.
  The upstream sources show that this Compose version grants Buildx read
  access only to build contexts. Buildx therefore refuses the shared
  Ollama Dockerfile, which was outside its context and the handoff's
  working directory. This cause was derived from the sources and not
  reproduced locally.
  [Release workflow](../.github/workflows/release.yml)

## 2026-11-25 22:17 CET — Bundled Ollama build recipes

- Packaged the compact CPU and CUDA 13 recipes in the Dockerfile shared by
  the Ollama downloader and serving images. `loaf build` and `loaf upgrade`
  use the selected recipe. INIs that use the earlier local test tags select
  their bundled equivalents. [Ollama build selection](configuration.md#select-an-ollama-build-recipe)
- The release chat and cold handoff checks build through the candidate's
  packaged recipe. [Verification](verification.md#fast-chat-check-with-compact-images)

## 2026-11-25 22:01 CET — Backup compression

- Set gzip level 4 for deployment backups and model-only exports. The
  release handoff uses the candidate's default unless given an explicit
  level. Recompressed the retained handoff fixture with the same tar
  payload. [Transfer guide](transfer.md#archive-contents) ·
  [Release acceptance](verification.md#check-a-release-candidate)

## 2026-11-25 21:55 CET — Compact inference and release handoff

- Added small CPU and CUDA 13 Ollama test bases and pinned Open WebUI slim
  for authenticated SmolLM2 chat checks. Annotated the compact and full
  settings with measured image sizes. The full model suite also checks
  WebUI authentication and model discovery.
  [Compact image checks](verification.md#fast-chat-check-with-compact-images)
- Added a standalone release handoff check for GitHub and Gitea. It uses a
  disconnected Debian Docker daemon, restores a synthetic account and
  chat from read-only media, and exercises a corrupt model blob.
  [Release acceptance](verification.md#check-a-release-candidate)
- Made cleanup refuse an invalid configuration so it can protect a custom
  terminal key. Added a shared compatibility test matrix for launcher and
  provisioner key readers. [Cleanup](launcher.md#clean-generated-files) ·
  [Terminal key format](terminal-server.md#enable-and-connect)

## 2026-11-25 20:58 CET — Documentation and architecture audit

- Documented the Compose service and network topology, CPU and NVIDIA
  serving, and model store layouts. [Architecture](instructions/architecture.md)
- Clarified Open Terminal package installation, which files survive runner
  replacement, and what transfers with a deployment backup.
  [Chat packages](terminal-server.md#install-packages-in-a-chat) ·
  [Backup contents](transfer.md#archive-contents)
- Corrected archive verification and restore behavior, and documented the
  loopback publishing limitation on older Docker Engine releases.
  [Restore behavior](transfer.md#restore-behavior) ·
  [Local network access](local-network.md#remove-local-network-publishing)

## 2026-11-25 16:55 CET — Chat-scoped APT packages

- Added the `install_chat_packages` Open Terminal tool. It runs APT in a
  disposable installer and publishes the successful result as a local
  image for the authenticated chat. Both working directories use that
  image. A new chat starts from the shared base image.
- Consolidated terminal builds and runtime package installs under
  `apt_source_file`. A repository on disk can use `apt_repository_dir`
  and a network-disabled installer.
- Saved chat images survive terminal restarts and are removed when the
  chat is deleted. Temporary chat images follow workspace cleanup.
  [Chat packages](terminal-server.md#install-packages-in-a-chat)

## 2026-11-25 15:52 CET — Edit terminal APT sources at runtime

- Added an optional host APT source file that Open Terminal workspaces
  mount read-only using the image already loaded on the Docker host.
  In-place edits are visible immediately. Running `loaf offline` after
  a file replacement recreates the affected containers.
  [Runtime APT sources](terminal-server.md#edit-an-apt-source-on-the-offline-host)

## 2026-11-25 15:37 CET — Select backup and restore components

- Added selectable images, volumes and project payloads. Restore verifies
  every archived payload and applies all present components when no names
  are supplied. [Component selection](transfer.md#select-archive-types)
- Kept the archived terminal key with the volumes component and covered
  successive component restores from the same retained bundle.
  [Terminal transfer](terminal-server.md#offline-transfer-and-shutdown)
- Added `backup volumes --models-only` and `restore volumes --models-only`
  through the existing rsync implementation. Model restore merges by default
  and accepts `--delete` for mirroring.
  [Model-store transfer](transfer.md#transfer-models-between-existing-deployments)

## 2026-11-25 15:19 CET — Retire old Loaf formats and test CPU workflows

- Removed the pre-release Loaf configuration, environment, bundle, and
  terminal-server fallbacks. Restore now requires the payload SHA-256 manifest.
  [Backup and restore](transfer.md#verify-a-backup)
- Kept numbered bundles out of later checkout snapshots and recovered saved
  settings when the operator selects a full archive by filename.
- Added CPU execution for the Docker model tests and for Modelfile replay
  after restore. Added regression coverage for the retired formats, archive
  selection, saved configuration, and CPU options.
  [CPU test workflow](verification.md#full-isolated-gpu-workflow)

## 2026-11-25 14:44 CET — Terminal key setup and in-place backup recovery

- Setup creates the configured bearer key with owner-only permissions and
  preserves an existing key. Added `setup --terminal-server-key` for key-only
  setup. Setup from a backup reuses its archived credential when available.
  [Terminal setup](terminal-server.md#enable-and-connect)
- Added `setup --all` to restore a source checkout and its Docker data from a
  copied backup. Both in-place restore paths retain the bundle under
  `backup/` and link the root launcher to `dist/loaf`.
  [Backup recovery](transfer.md#debian-restore)

## 2026-11-24 00:43 CET — Open Terminal workspace labels

- Adopted `loaf.*` Docker ownership labels and `loaf-tools-` resource names in
  the provisioner and backup tooling. Documented the effect on workspaces
  from earlier prerelease builds. [Terminal deployment](terminal-server.md#upgrade-an-existing-terminal-deployment)
- The connected and offline fixtures use the backup volume inventory to check
  that both account volumes are discoverable before and after restart.

## 2026-11-24 00:06 CET — Local network access guide

- Added the operator responsibility warning and instructions for publishing
  and removing WebUI access on native Debian and on Windows with WSL 2.
  The WSL guide covers NAT port forwarding, mirrored networking and reversal.
  [Local network access](local-network.md)

## 2026-11-23 22:51 CET — Clean the current installer layout

- Limited `loaf clean --full` to current installation and build paths.
  Updated the cleanup fixture to use `install-cache/` and check Git work
  inside generated directories. [Cleanup](launcher.md#clean-generated-files)

## 2026-11-23 22:33 CET — GitHub quick release guide

- Added the short GitHub workflow for the `v0.1.0` release, from staging the
  tracked changes through publishing the draft. [Quick release](releases.md#quick-release)

## 2026-11-23 22:20 CET — Public checkout ignore rules

- Grouped ignore rules for Python and launcher output, local credentials,
  transfer data, and artifacts from earlier releases. Covered all launcher
  staging directories, `.env` variants, and custom `.key` files under
  `config/`. Kept `.env.example` visible for sample configuration.

## 2026-11-23 22:01 CET — Optional Ollama tool history compatibility

- Added a toggleable Open WebUI request filter for interrupted Ollama tool
  turns. It closes each unfinished turn with an empty assistant message in
  the outgoing request and keeps completed tool calls and results available.
- Administrators select the filter per model and can set a default for new
  chats. Each chat can also opt in from the input menu.
  [Setup and troubleshooting](troubleshooting.md#conversation-roles-must-alternate)

## 2026-11-23 17:43 CET — Include standalone license notices and sources

- Standalone and tagged builds generate one `LICENSE.txt` with the project's
  BSD terms, the bundled notices and Berkeley DB's corresponding source
  information. The executable prints the same file with `./loaf --license`.
- The GitHub and Gitea release jobs attach the file with the binary. The
  tagged build includes it in `SHA256SUMS`. [Standalone license](releases.md#standalone-license)

## 2026-11-23 17:29 CET — Show the latest run time on test badges

- Added the local run time to each refreshed badge and its README text.
  Repeat runs with the same result now show when they ran. Runs recorded in
  the same second also show their run number. [Test badges](verification.md#readme-test-badges)

## 2026-11-23 17:24 CET — Public-facing documentation review

- Clarified the connected and offline workflows at the top of the README.
- Kept the factual AI assistance disclosure, clarified the scope of the
  project's license, and corrected the manual release example.

## 2026-11-23 17:16 CET — Include the available terminal image in backups

- Backup saves the local Open Terminal image for the selected deployment when
  it exists. `loaf backup --no-terminal-server` skips that image.
- Workspace volumes and the bearer key follow `[terminal_server] include_in_backup`.
  Verification and restore detect the image and workspace data independently.
  [Archive contents](transfer.md#archive-contents)

## 2026-11-23 17:11 CET — Local AI Ferry branding

- Renamed the project to Local AI Ferry, the Python distribution to
  `local-ai-ferry`, and the CLI, Python package and standalone executable to
  `loaf`. Updated release assets, transfer names, examples and CI workflows.
- Restore and model import recognize earlier archive names and manifests.
  Compose keeps its deployment resource identities and terminal image contract.
  Setup and link commands can refresh Docker links from earlier releases.
  [Launcher](launcher.md) · [Transfer](transfer.md)

## 2026-11-23 15:45 CET — Command and flag help

- Top-level help lists commands before global flags. Each command and host
  integration has scoped help based on the launcher's flag definitions.
- Added a short [CLI overview](cli.md) and a complete [command reference](cli-help.md).
  The launcher guide focuses on installation and deployment workflows.

## 2026-11-23 15:32 CET — One-command release tagging

- Added a release helper that derives the annotated tag from the committed
  package version and pushes it to the chosen Git remotes. It supports a
  preview, inline or file-based notes, and retries with the original tag.
- Documented the helper alongside the standard branch, tag, draft review,
  and publication workflow. [Release guide](releases.md#push-a-version-tag)

## 2026-11-23 15:21 CET — Cleanup and safety review

- Added `loaf clean` for temporary files and the shared runtime cache.
  `--full` also clears installation caches and generated installation
  files. Both modes preserve Git, backup archives and deployment data.
  [Cleanup](launcher.md#clean-generated-files)
- Shared the runtime lock between packing and launching. Hardened project
  locks against links and kept standalone systemd commands tied to the
  original executable so they can recreate the runtime after cleanup.
- Made setup reject symlinked configuration directories and checked
  volume archive paths and links before Docker restore.
  [Restore behavior](transfer.md#restore-behavior)

## 2026-11-23 14:13 CET — Reuse cached build wheels and simplify output

- The standalone build checks `install-cache/wheels/` for compatible
  build requirements, including setuptools, before fetching them.
- Binary, backup and tagged builds report short progress messages and
  display the captured pip or executable check output on failure.
  [Local binary build](releases.md#build-and-check-locally)

## 2026-11-23 14:04 CET — Local binary builds

- Added `loaf binary` to build or update `dist/loaf` from working source files
  using the shared release build script.
- Source checkout backups build and bundle the current executable before
  accessing Docker. Standalone deployments package their running executable.
  [Local binary build](releases.md#build-and-check-locally) ·
  [Backup contents](transfer.md#archive-contents)

## 2026-11-23 13:15 CET — Self-contained backup folders

- Full backups now publish a dated folder with the matching data archive,
  standalone executable and verification manifest.
- The release and backup executables use the same minimal setup. Full
  restore discovers the bundle, checks its files and recovers saved
  settings and Modelfiles with the Docker data. Model import continues
  to use model-only archives. [Offline transfer](transfer.md#debian-restore)

## 2026-11-23 12:55 CET — Standalone release executable

- Added a Linux amd64 `loaf` release executable with CPython, the launcher,
  deployment files, and Python build wheels. Setup in a fresh directory
  creates a project with a local Git snapshot of the bundled source.
- Both release workflows attach the executable, a separate complete
  source archive, a Python wheel, and checksums.
  [Release guide](releases.md)

## 2026-11-23 12:44 CET — Installer cache and releases

- Moved generated wheels and Debian installers to the Git-ignored
  `install-cache/` directory and updated the project payload in new
  backups. The earlier installer paths are outside the new cache layout.
- Added `loaf setup --with-docker-deps` on Debian 13 to cache and install
  Debian Docker packages. An existing Docker installation can prepare a
  complete package set for an offline target. [Debian setup](debian.md#install-docker-and-cache-its-packages)
- Added GitHub and Gitea workflows that build and attach the wheel, source
  distribution and checksums to a draft for a matching version tag.
  [Release guide](releases.md)

## 2026-11-23 11:20 CET — Concise GPU readiness error

- Shortened the NVIDIA probe failure in `loaf status` to one line and
  included the CPU readiness command.
  [Readiness checks](launcher.md#check-readiness-with-status)

## 2026-11-23 10:48 CET — Refresh badges from the test runner

- Added `loaf test --badges`. It records executed cases, regenerates the
  corresponding SVGs and updates their README links. Badge totals use
  the cases executed in the current run, including failures. Fully
  skipped groups retain their previous results. [README test badges](verification.md#readme-test-badges)

## 2026-11-23 10:37 CET — Run dates on README badges

- Added the last recorded run date below each test group name. Kept the
  pass count on the right and documented the manual refresh process.
  [README test badges](verification.md#readme-test-badges)

## 2026-11-23 10:24 CET — README test badges

- Added local SVG badges for the CLI baseline and the Docker, Open
  Terminal, GPU inference, and model catalog groups. Each badge shows the
  passed and total counts for its group and links to a dated development-PC
  result. The verification guide gives separate test selectors and explains
  how to refresh the counts. [README test badges](verification.md#readme-test-badges)
- Followed Open MCT's separation of unit and end-to-end suites and
  genbadge's practice of linking test-count badges to their reports.
  [Open MCT test organization](https://github.com/nasa/openmct#tests) ·
  [genbadge test badges](https://smarie.github.io/python-genbadge/#1-tests-badge)

## 2026-11-23 10:09 CET — CPU serving on preparation hosts

- Added `--cpu` to `loaf online` and `loaf offline` for hosts without NVIDIA
  access. CPU serving uses the existing deployment images and volumes, and
  the launcher selects GPU inference by default.
- Added `loaf status --cpu` to check readiness for the same serving mode.
  The CPU Compose override ships with the launcher wheel.
  [CPU serving](launcher.md#serve-on-cpu)

## 2026-11-22 21:33 CET — Update the architecture guide

- Documented Open Terminal routing, workspace isolation, the working directory
  picker, storage lifetimes and chat deletion. Updated the build, shutdown,
  integration and backup descriptions. [Architecture](instructions/architecture.md)

## 2026-11-22 21:28 CET — Default entries in the working directory menu

- Supplied `user` and `chat` as direct working-directory choices in the
  bundled terminal. The provisioner provides the two records alongside
  Open WebUI's per-account folder history.
  [Terminal workspace folders](terminal-server.md#use-the-terminal-from-a-model)

## 2026-11-22 21:11 CET — Chat folders in the working directory picker

- Made `/home` the file browser root for drafts so the native **Working
  Directory** picker can select `chat` before sending the first message.
  The provisioner attaches writable storage when the chat starts.
  [Terminal workspace folders](terminal-server.md#use-the-terminal-from-a-model)

## 2026-11-22 21:03 CET — Find chat folders from the Files tab

- Documented reopening Files after a draft becomes a saved chat so the tab
  picks up the chat workspace and its parent breadcrumb.
  [Terminal workspace folders](terminal-server.md#use-the-terminal-from-a-model)

## 2026-11-22 20:53 CET — Chat folders in Open Terminal

- Kept the existing per-account volume at `/home/user` and exposed both the
  user and chat directories through the Files breadcrumb. Model commands and
  file requests in a chat route to the same runner.
- Added a private `/home/chat` volume for each saved chat and temporary
  storage for unsaved sessions. The provisioner reads Open WebUI's SQLite
  database through a read-only mount and removes saved chat volumes after
  deletion. Archived chats retain their files.
  [Terminal workspace folders](terminal-server.md#use-the-terminal-from-a-model)

## 2026-11-22 12:52 CET — Separate third-party notices

- Moved upstream license annotations to `THIRD_PARTY_NOTICES.txt` and shipped
  it alongside the BSD license in the wheel.
  [GitHub license detection](https://docs.github.com/en/rest/licenses/licenses#about-licenses)
  · [Python distribution notices](https://packaging.python.org/en/latest/specifications/pyproject-toml/#license-files)

## 2026-11-22 12:50 CET — Third-party license notices

- Filled the third-party license header with upstream notices for the
  deployment tools, integrations and configured model downloads. Matched
  the repository and Python package license files.

## 2026-11-22 12:40 CET — GitHub working-tree quality audit

- Updated the README, added contributor and private reporting guidance,
  and added a read-only GitHub Verify workflow with pinned actions. The
  workflow checks Python 3.10 and 3.13 and smoke-tests the installed wheel.
  [README guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)
  · [Contribution guidance](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions)
  · [Workflow security guidance](https://docs.github.com/en/enterprise-cloud%40latest/code-security/tutorials/secure-your-organization/protect-against-threats)
- Included the BSD 3-Clause notice and repository metadata in the Python
  distribution. An installed-package test compares the shipped notice
  with the repository's license.
  [Python packaging metadata](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)

## 2026-11-22 12:33 CET — Independent Docker checks and quieter test output

- Split Compose, image, shell, GPU and terminal checks into named cases.
  GPU serving and transfer cases copy one stopped model store into separate
  project volumes. Added real coverage for stop behavior, volume retention
  and deployment scope, and strengthened the terminal migration and invalid
  Modelfile checks.
- Buffered test output and captured Docker command streams for failure and
  timeout diagnostics. Added an example of running one Docker case by name.
  [Verification guide](verification.md#docker-configuration-and-real-shell-scripts)

## 2026-11-22 12:13 CET — Documentation audit and full verification

- Audited the README and guides against the CLI, packaged Compose files,
  configuration, transfer code and test fixtures. Updated installation and
  recovery steps, status and shutdown behavior, model selection, desktop
  integration, terminal isolation and test prerequisites.
  [Launcher](launcher.md) · [Verification guide](verification.md)

## 2026-11-22 11:58 CET — Grouped configuration and Open Terminal image checks

- Grouped launcher and Compose configuration with the terminal bearer key.
  Moved the Open Terminal image into the common local image checks.
  [Status report](launcher.md#check-readiness-with-status)

## 2026-11-22 11:52 CET — Short flag for keeping stopped containers

- Added `loaf stop -n` as the short form of `--no-remove-containers`.
  [Launcher](launcher.md#stop-and-remove-containers)

## 2026-11-22 11:52 CET — Remove service containers by default

- Made container removal the default for `loaf stop` and added
  `--no-remove-containers` to keep stopped containers. [Launcher](launcher.md#stop-and-remove-containers)

## 2026-11-22 11:51 CET — Compose compatibility warning

- Changed Compose 2.26.1 through 2.29.x from a readiness failure to a
  compatibility warning. [Status compatibility](launcher.md#check-readiness-with-status)

## 2026-11-22 11:50 CET — Optional container removal

- Added opt-in container removal to `loaf stop` using Compose `rm --force`
  after stopping the deployment. [Launcher](launcher.md#stop-and-remove-containers)

## 2026-11-22 11:48 CET — Docker Engine and Open Terminal status

- Made `loaf status` warn on Docker Engine 26 and 27 and report Open Terminal
  as `-`, `BUILT`, or `OK`. [Readiness guide](launcher.md#check-readiness-with-status)

## 2026-11-22 11:44 CET — Ollama model catalog readiness

- Updated `loaf status` to detect Ollama's `manifests-v2` catalog and follow
  manifest links to existing blobs. [Readiness guide](launcher.md#check-readiness-with-status)

## 2026-11-22 11:39 CET — Status report layout

- Added deployment details, grouped readiness checks, a container heading and
  a preflight summary to `loaf status`. [Launcher](launcher.md#check-readiness-with-status)

## 2026-11-22 11:32 CET — Tether installation command

- Added `loaf integration tether` to print desktop installation and sign-in steps
  with the official releases URL. [Tether guide](tether.md)

## 2026-11-22 11:18 CET — Host integration commands and Tether

- Grouped the existing configuration generators under `loaf integration systemd`
  and `loaf integration continue`. Updated the launcher examples and added a
  desktop installation and connection guide. [Host integrations](installation.md#host-integrations)
  · [Open WebUI Tether](tether.md)

## 2026-11-22 01:27 CET — Terminal backup and package mirrors

- Added `[terminal_server] include_in_backup` for the terminal image, the
  selected project's workspace volumes, and the bearer key. Restore
  discovers these assets in the archive, recreates workspace ownership
  labels, and restores the key with owner-only permissions.
  [Terminal transfer](terminal-server.md#offline-transfer-and-shutdown)
- Moved the terminal base image to `[images] terminal_server_image` and kept
  compatibility with the former `base_image` setting. Added
  `[terminal_server] apt_source_file` for local package mirrors during image
  builds. [Package mirror](terminal-server.md#package-mirror-for-image-builds)

## 2026-11-22 00:58 CET — Terminal server rename and migration

- Renamed the service and build context to `terminal-server`, the Compose
  profile to `terminal`, and the INI section to `[terminal_server]`.
  The launcher stops the former service during a transition. Existing
  workspace volumes, legacy key paths, and saved terminal URLs can be
  reused. [Terminal server upgrade](terminal-server.md#upgrade-an-existing-terminal-deployment)
- Added bearer key format checks to serving startup and readiness. A new
  account can reclaim an eligible idle slot immediately when the runner
  limit is reached. Active requests and processes keep their runners.
  [Workspace policy](terminal-server.md#workspace-policy)
- Updated the setup, configuration, transfer and verification guides with
  the current defaults and the terminal server's prerequisites.

## 2026-11-22 00:42 CET — Default workspace Home

- Set Home explicitly as the Open Terminal working directory and the
  Open WebUI Files root for each runner. Added coverage of the writable
  Home and independent chat folder selections within one account's
  workspace. [Tool server setup](terminal-server.md#use-the-terminal-from-a-model)

## 2026-11-22 00:09 CET — Ministral with Open Terminal

- Documented model-level terminal settings and a GCC prompt tested with
  the local Ministral model through Open WebUI's native tool loop.
  [Use the terminal from a model](terminal-server.md#use-the-terminal-from-a-model)

## 2026-11-21 23:59 CET — Ollama v2 model archives

- Model export and import now validate Ollama's `manifests-v2` registrations,
  including relative tag links, manifest-blob hashes, manifest lists, and
  referenced blobs. The archive preserves the v2 links.
  [Model transfers](transfer.md#transfer-models-between-existing-deployments)

## 2026-11-21 23:48 CET — Modelfile checks during backup

- Full backups compare saved Modelfiles with the current Ollama definitions
  through a read-only, network-disabled container. They report differing
  definitions, missing referenced blobs, and unverified definitions before
  publishing the captured archive. [Modelfile checks](transfer.md#check-saved-modelfiles-during-backup)

## 2026-11-21 23:15 CET — Production tool server on Docker Engine 26+

- Lowered the tool server's Engine minimum to 26. On Engine 26 and 27,
  the provisioner creates internal workspace bridges with
  `inhibit_ipv4=true`. Engine 28+ uses isolated gateway mode.
- Disabled IPv6 inside runners and added acceptance checks for host
  bridge addresses, runner routes, peer access, and access to the
  provisioner's control API. [Tool server setup](terminal-server.md)

## 2026-11-21 23:02 CET — Tool-server tests on older Docker Engines

- Added `[tests] allow_legacy_tool_bridge` to run the disposable tool-server
  test with ordinary internal workspace bridges on Docker Engine below 28.
  The test inspects each workspace bridge. Normal tool-server startup still
  requires an Engine that supports isolated bridges.
  [Tool server acceptance checks](terminal-server.md#acceptance-checks)

## 2026-11-21 22:53 CET — Open WebUI tool workflows

- Changed the tool-server acceptance test to use Open WebUI's saved direct
  connection and authenticated accounts in both serving profiles. The fixture
  tests access grants, verified workspace ownership, and cold restart after
  removing the online service's extra network attachment.
- Added a GCC/G++ and Python edit-and-rebuild workflow and a Sphinx upload,
  build, download, and revision workflow. The Compose contract now checks
  the NVIDIA device reservation rendered by the installed Compose.
  [Tool server acceptance checks](terminal-server.md#acceptance-checks)

## 2026-11-21 22:38 CET — Optional per-user tool server

- Added the Open Terminal provisioner, a shared runner image, and a
  `[tool_server]` section in `config.ini`. Compose mounts the bearer key from
  the configured file. The launcher builds and starts the provisioner when
  enabled and includes it when stopping the deployment.
- Added workspace ownership, private networks, persistent volumes, idle
  cleanup, and command limits. [Tool server setup](terminal-server.md)

## 2026-11-21 22:17 CET — Tool server setup guide

- Condensed the tool server guide to the selected per-user deployment,
  configuration, provisioning, and acceptance checks. [Tool server setup](terminal-server.md)

## 2026-11-21 22:15 CET — Open Terminal design selection

- Selected one pinned Open Terminal image with a separate container and
  persistent workspace per WebUI account. The administrator manages one
  direct connection and its access grants in Open WebUI.
  [Selected design](terminal-server.md#selected-design)

## 2026-11-21 22:06 CET — On-demand terminals per Open WebUI user

- Traced user identity and terminal routing through Open WebUI's HTTP,
  tool-calling and WebSocket paths. Compared the licensed Docker orchestrator
  with community implementations and documented a fee-free Docker proposal
  using Open WebUI's direct terminal integration. [Tool server proposal](terminal-server.md)

## 2026-11-21 21:53 CET — Tool server research

- Evaluated fee-free tool execution and multi-user deployment options.
  Proposed per-user Open Terminal services for the initial evaluation and
  recorded the commercial licensing and isolation trade-offs.
  [Tool server proposal](terminal-server.md)

## 2026-11-21 21:42 CET — Consolidated CLI functionality

- Implemented model archive transfer with `loaf export` and `loaf import`.
  Import merges by default and mirrors the archive with `--delete`.
  [Model transfer](transfer.md#transfer-models-between-existing-deployments)
- Implemented readiness checks in `loaf status` and Docker-free archive checks
  in `loaf backup --verify`. [Readiness](launcher.md#check-readiness-with-status) ·
  [Archive verification](transfer.md#verify-a-backup)
- The overlap between stored Ollama manifests and reapplying saved Modelfiles
  at serving startup is open for a design decision.

## 2026-11-21 21:42 CET — Model transfers and readiness

- Consolidated model archive transfer under `loaf export` and `loaf import`.
  The downloader stops before transfer and the serving profile can remain up.
  Import merges by default. `--delete` removes destination-only model data.
  [Model transfer](transfer.md#transfer-models-between-existing-deployments)
- Added Compose, image, GPU, volume and port checks to `loaf status`.
  [Readiness checks](launcher.md#check-readiness-with-status)
- Added SHA-256 digests to full backups and Docker-free verification with
  image platform reporting. [Archive verification](transfer.md#verify-a-backup)

## 2026-11-21 18:22 CET — NVIDIA installer cache

- Moved the NVIDIA Toolkit cache to `python-package/deb/`. Backup preserves
  the installer set there, and setup uses recovered files with APT sources
  disabled. [Disconnected installation](installation.md#disconnected-debian-installation)

## 2026-11-21 18:15 CET — Foreground log following

- Added `--attach` / `-a` for Compose workflows. The generated `Type=exec`
  systemd unit starts `loaf offline --attach` and stops the same deployment.
  [Launcher options](launcher.md#options)

## 2026-11-21 18:07 CET — Generated host integrations

- Replaced static examples with `loaf systemd-service` and `loaf continue-config`.
  The unit uses the active Python environment and deployment. Continue YAML
  uses the configured WebUI address and selected models. [Host integrations](installation.md#host-integrations)

## 2026-11-21 17:59 CET — Local configuration overrides

- Kept defaults in the Python package and added independent starter files in
  `config/`. The launcher applies local settings over the packaged values and
  selects local model files first. [Configuration guide](configuration.md#project-configuration)

## 2026-11-21 17:47 CET — Checkout configuration

- Kept defaults in the top-level `config/` directory and linked the source
  deployment tree to it. Wheels ship the same files, including the example
  selections under `config/model-examples/`. [Configuration guide](configuration.md#project-configuration)

## 2026-11-21 17:41 CET — Packaged deployment data

- Packaged Compose, the Docker build contexts and default configuration in
  `loaf/data`. Added `loaf link-docker-project` for direct Compose use from an
  editable or wheel installation. [Direct Compose commands](launcher.md#link-the-docker-project)

## 2026-11-21 17:32 CET — Docker build context relocation

- Moved the three build contexts under `docker/`. The root `compose.yml`
  discovers the new paths and uses the default volume names when the namespace
  is unset. [Architecture](instructions/architecture.md#cli-and-compose-profiles)

## 2026-11-21 17:13 CET — Test suite relocation

- Moved the Python suite and its shell fixtures to `python-package/tests/`.
  Updated `loaf test` discovery, direct unittest commands and fixture paths.
  [Verification guide](verification.md)

## 2026-11-21 17:07 CET — Wheel reuse and setup output

- Reused the wheel cache across bootstrap, setup and backup when its inputs
  and artifacts match. Setup reuses a current editable install and prints pip
  output when a Python preparation step fails.
  [Python wheel preparation](launcher.md#prepare-python-installation-offline)

## 2026-11-21 17:00 CET — Command output and progress

- Made Compose command traces opt-in with `-v`. Default output describes
  workflow steps, and service startup reports elapsed time during long waits.
  [Launcher options](launcher.md#options)

## 2026-11-21 16:53 CET — Installation-only setup

- `loaf setup` and `loaf install` now use the same preparation workflow. Quick
  Setup shows build or upgrade, download, offline verification and backup as
  separate commands. [Quick Setup](../README.md#quick-setup)

## 2026-11-21 16:49 CET — One-command Quick Setup

- Added `loaf setup` to check Docker, install the launcher, build images,
  prepare models and start offline serving. The launcher holds one checkout
  lock and reports the stage if a step fails. [Quick Setup](../README.md#quick-setup)
- Retired the native Windows and PowerShell guides and shell checks. The
  current instructions use Bash on native Debian or Debian in WSL2.

## 2026-11-21 16:42 CET — Configuration and model-only transfer

- Moved launcher settings, named model INIs and integration examples to
  `config/`. Added `--config-dir`, retired plain-text model lists and added
  Ollama aliases that WebUI can display. [Configuration](configuration.md#project-configuration)
- Added `loaf models backup` and `loaf models restore` for existing deployments.
  Restore stages and validates the archive before merging it with `rsync`.
  [Model transfer](transfer.md#transfer-models-between-existing-deployments)
- Replaced personal names and machine-specific paths in the working files.
  Historical Git commits still contain their original attribution.

## 2026-11-21 14:07 CET — Python wheel preparation and backup

- `loaf install` resolves the package's declared build and runtime requirements,
  writes wheels to `python-package/wheels`, and installs from the local cache.
  Backup rebuilds the launcher wheel from the checkout and includes the wheels
  in `project.tar`. [Python wheel preparation](launcher.md#prepare-python-installation-offline)
- A virtualenv with an unavailable base Python is detected before packaging.
  The installer can rebuild it using the host's Python.

## 2026-11-21 13:47 CET — INI model selection

- Added `models.ini` with the tested default in `[models.default]`. A selected
  model file supplies the whole selection. Named INI sections replace the
  default, and existing text lists are supported. [Model selection](launcher.md#select-models)

## 2026-11-21 13:33 CET — Project INI configuration

- Added per-checkout defaults for deployment and model selection, base images,
  serving settings, backups and test opt-ins. The launcher validates the file
  before invoking Docker. Existing shell and `.env` overrides still work.
  [Configuration guide](configuration.md#project-configuration)

## 2026-11-21 13:06 CET — Debian installer and end-to-end transfer

- Added `loaf install` to prepare or repair `.venv`, install the Debian
  NVIDIA toolkit when absent, and cache the matching installer packages.
  Backup includes `.venv` and those packages under `nvidia-ctk/`. Debian
  with Docker Engine is now the recommended setup on native hosts and WSL2.
  [Installation](debian.md#install-the-loaf-command) · [Backup and transfer](transfer.md)
- Validated build, upgrade, backup, restore, connected startup and offline
  GPU inference in a disposable namespace using a second checkout on the
  same Docker host. [Verification record](verification-record.md)
- Evaluated follow-up commands. A read-only `loaf doctor` would catch missing
  Compose and Buildx prerequisites before a workflow starts. An archive
  verification command could reuse the layout, checksum and image-tag checks
  without loading images or writing volumes. [Development backlog](instructions/development.md)

## 2026-11-21 — Legacy documentation cleanup

- Removed the manual three-archive restore procedure and its cross-references.
  The active guides now explain the single-archive `loaf backup` and
  `loaf restore` workflow.
- Removed examples that depended on an old local model alias.

## 2026-11-21 12:34 CET — Versioned deployments

- Added `loaf --version VERSION` to scope Compose projects, image tags,
  volumes and backup filenames. Commands without a version keep the
  existing default names.
- Restore selects the newest backup in the requested namespace and checks
  its image tags before loading images or restoring volumes.
  [Versioned deployments](launcher.md#run-a-versioned-deployment) ·
  [Versioned transfers](transfer.md#versioned-transfers)

## 2026-11-21 12:29 CET — Repository and verification cleanup

- Made `loaf test` fail when it discovers no tests. Replaced the old archive
  fixture with a Docker round trip using the production volume commands.
  Isolated GPU checks reuse cached model data through a read-only mount.
- Bounded Ollama startup probes, cleared stale readiness markers, and
  prevented an export from treating a directory as its destination.
  Backup now checks the checkout and saves images before switching profiles.
- Updated the systemd unit to use `loaf`, refreshed the Continue model
  configuration, and removed the CI checkout step that disabled TLS
  verification. [Host integrations](installation.md#host-integrations)

## 2026-11-21 11:39 CET — Transfer and first-run fixes

- Excluded working-tree copies of `offline-images.tar` from `project.tar`.
  Added opt-in `backup --export` and `restore --import` steps under the
  transfer lock.
- Clarified how to invoke `loaf` after an editable install, including direct
  calls to the virtualenv executable and shell activation.
  [Backup and transfer](transfer.md) · [Launcher installation](launcher.md#install-the-package)

## 2026-11-21 11:28 CET — Documentation consolidation

- Moved implemented features into Architecture and kept pending work in
  Development. Consolidated verification instructions and coverage in
  `verification.md` and moved dated results into `verification-record.md`.
- Added the root `AGENTS.md` documentation map. Updated Debian instructions
  for users with Docker group access and documented that Git-ignored NVIDIA
  packages travel separately. [Architecture](instructions/architecture.md) ·
  [Verification guide](verification.md) · [Repository guide](../AGENTS.md)

## 2026-11-21 10:49 CET — NVIDIA packages removed from Git

- Commit `33a85c2` removed the offline NVIDIA Toolkit packages from tracking
  and added `nvidia-offline/` to the ignore rules.
  [Disconnected Debian installation](installation.md#disconnected-debian-installation)

## 2026-11-21 10:47 CET — v0.4 command and transfer workflows

- Commit `e98e317` split model preparation into `download` and `export`,
  added connected `online` serving alongside `offline`, and introduced
  standalone `build`, `upgrade`, `status` and `stop` commands.
- Added `backup` and `restore` for a single archive carrying the built
  images, both named volumes and the Git checkout. Added transfer progress,
  regression tests and initial systemd and Continue examples.

## 2026-11-16 22:52 CET — Python launcher and automated tests

- Commit `eb2809c` introduced the editable `local-ai-ferry` Python
  package, the `loaf` and `python -m loaf` entry points, and `loaf test`.
- Added model selection files, explicit import, automated workflow checks,
  and dedicated installation and operating guides.

## 2026-11-16 20:55 CET — Initial Compose deployment

- Commit `e883c6c` introduced the Ollama downloader and exporter, the offline
  NVIDIA inference service, and Open WebUI. It documented the manual model
  preparation and transfer workflows and recorded the first Windows 11
  Docker Desktop and WSL2 checks.
