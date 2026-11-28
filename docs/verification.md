# Verification guide

[Project overview](../README.md)

Run the tests in Debian or Debian in WSL2, from the project directory with the
[editable package installed in `.venv`](installation.md#source-checkout).
The suite uses Python's standard `unittest` runner.
See the [verification record](verification-record.md) for dated results.
Run `./loaf setup tests` once to install the packages for the
terminal server API tests.

[Test packages](installation.md#test-packages)

## Run the fast suite

```bash
./loaf test
./loaf test -v
```

The default report shows the count, duration, outcome and skips. Use `-v`
to see each test name and result. The runner buffers test output, and Docker
checks capture command output. Failures show tracebacks and captured
diagnostics and return a nonzero exit status.
An empty suite exits with status 5.

The command discovers [`python-package/tests/`](../python-package/tests/) in the
current checkout. Use `--project-dir PATH` to select another checkout. To run an
individual group from the checkout root, use standard unittest discovery.

```bash
.venv/bin/python -m unittest discover -s python-package/tests -t python-package -p 'test_package.py' -bv
```

Run documentation checks separately when editing a guide.

```bash
(cd python-package && ../.venv/bin/python -m unittest tests.test_documentation)
```

## Debian smoke checks

Run these checks after [starting the offline stack on Debian](../README.md#quick-setup).
Use a login with [Docker access](debian.md#docker-access). These commands
use the default namespace and port.
For a configured deployment, `loaf status` checks serving prerequisites and
lists its containers. `loaf run offline` prints its browser address.

```bash
./loaf docker ps
docker exec loaf-ollama-offline-1 nvidia-smi
docker exec loaf-ollama-offline-1 ollama list
curl -fsS http://localhost:3000/health
```

After sending a message, inspect the active model.

```bash
docker exec loaf-ollama-offline-1 ollama ps
```

Look for `100% GPU`. An empty list means no model is currently loaded.

On a host without NVIDIA GPU access, run `loaf status --cpu` and
`loaf run offline --cpu` or `loaf run online --cpu`. Check WebUI health and send a chat
message. Ollama's process list should report `100% CPU` while the model is
loaded.

[CPU serving](launcher.md#serve-on-cpu)

## Docker configuration and real shell scripts

Build the images while connected, then enable the Docker checks.

```bash
./loaf build
RUN_DOCKER_TESTS=1 ./loaf test -v
```

Use a local Docker daemon and the three project images. The Compose contract
checks the rendered NVIDIA device reservation. The launcher readiness check
warns on Compose 2.26.1 through 2.29.x and requires at least 2.26.1.

The shell cases mount the two production scripts read-only and run them
against a fake Ollama executable in a fresh container for each case. The
archive case uses the production volume transfer commands and checks filenames
with colons and binary data. The stop cases use the installed CLI against
disposable projects and check container IDs, volume contents, and that a
second deployment keeps running.

Select one case by name from the package directory. Use `-b` with direct
`unittest` runs to buffer test output as `loaf test` does.

```bash
(cd python-package && RUN_DOCKER_TESTS=1 ../.venv/bin/python -m unittest -bv \
    tests.test_docker.DockerTests.test_stop_removes_container_and_preserves_its_named_volume)
```

[Shell fixtures](../python-package/tests/script_checks.sh) ·
[Volume fixtures](../python-package/tests/archive_checks.sh)

## Full isolated GPU workflow

Use the built local images, working NVIDIA container access, internet access,
and disk space for the model and staged copies. Each Compose project lives in
`.test-artifacts/` in the checkout, on disk even where `/tmp` is a small
memory-backed filesystem. The transfer case needs about 6 GB there: backup
stages the uncompressed model store (2.8 GB) beside its compressed archive,
and restore stages the store again before it changes the volume. `loaf clean`
removes the directory. The suite downloads Ministral and exports its
Modelfiles once. Each serving workflow starts from
a copy in its own volume and Compose project.

```bash
RUN_DOCKER_TESTS=1 RUN_GPU_TESTS=1 ./loaf test -v
```

The individual cases check the source and alias Modelfiles, recovery of a
missing manifest, explicit replay of a new definition, and recovery from an
invalid definition that prevents WebUI startup. The inference case checks
authenticated model discovery through WebUI, the 16,384-token default
and full GPU placement. The transfer case switches
to the connected profile, checks WebUI health on a temporary loopback port,
and mirrors the model store with `--delete`. It verifies that the added
alias disappears and both connected containers keep their IDs.
Cleanup targets the generated projects and their disposable resources.

Use `loaf test --cpu` on a host with Docker and no NVIDIA GPU. It enables
the Docker suite and runs the same isolated model workflows with the CPU
Compose override. The inference check uses a short prompt and verifies CPU
placement and the configured context. The model download and disk
requirements apply to both modes.

```bash
RUN_TERMINAL_SERVER_TESTS=1 RUN_CATALOG_TESTS=1 ./loaf test --cpu -v
```

[GPU cases](../python-package/tests/test_docker_gpu.py)

## Terminal server checks

The optional [terminal server Docker checks](terminal-server.md#acceptance-checks) use
`RUN_TERMINAL_SERVER_TESTS=1`, Docker Engine 26+, and locally built
`loaf/open-webui:latest` and `loaf/open-terminal:latest` images from the current
checkout. Rebuild the terminal image after changes to the provisioner. The
serving case runs the scripts baked into the image and fails with a
rebuild hint when they differ from the checkout.
Use `LOAF_TEST_TERMINAL_IMAGE` to select a disposable test tag.
Run it on native Debian and Debian in WSL2 with the same local images
before and after host disconnection. The serving cases drive
Open WebUI's terminal proxy with two authorized accounts and a denied
account, exercise GCC/G++, Python, and Sphinx workflows, and repeat the
builds after a cold restart on the internal control network. The online and
offline cases have separate Docker projects and volumes and run
concurrently. They need no model or GPU.

The serving cases use `http://terminal-server:8000` for saved connections
and check the default and recent entries offered in the working-directory
menu, selection before the first message, and visibility of files created by
commands.
They also check chat isolation and container removal after chat deletion.
The suite includes a Docker backup round trip for a terminal image in a separate namespace,
the user volume with an account folder and its ownership, and an owner-only
bearer key, also with `terminal` alone.
The local APT case uses `dpkg-deb` to create a disposable repository. It checks
root commands that start in `/home/chat`, installation with `apt-get` and
`sudo apt-get` including a real dependency and post-install script, the
account's folder shared by its chats, isolation from a second chat and from
another account's draft, a temporary chat that starts fresh after a restart,
packages and files of a saved chat across the restart, and container removal
after chat deletion.

The serving cases exercise production isolation by default and use `iproute2` on
the local Docker host to inspect the bridge. It checks that the host bridge
has no IPv4 address and each workspace has no default route, IPv6 address,
or connection to the other workspace or control API. On Engine 26 and 27,
`[tests] allow_legacy_terminal_bridge = true` runs the workflows with ordinary
internal workspace bridges for comparison. Set
`ALLOW_LEGACY_TERMINAL_BRIDGE=1` for the same test-only override from the shell.

Local HTTP and WebSocket proxy tests require `aiohttp`. The local
command-timeout test requires the Open Terminal Python package pinned
in the terminal image and `httpx`. `loaf setup tests` installs these
versions. To test against the image's own interpreters instead, run both
groups inside the built terminal image. Its Debian Python runs the
provisioner and its Open Terminal Python runs the runner, with the
checkout mounted read-only and no network.

```bash
for test in /usr/bin/python3:ProvisionerTests /usr/local/bin/python:RunnerTests; do
    docker run --rm --pull never --network none --read-only --tmpfs /tmp \
        --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
        -e PYTHONPATH=/src/python-package/src:/src/python-package -w /src/python-package \
        --mount "type=bind,source=$PWD,target=/src,readonly" \
        --entrypoint "${test%%:*}" loaf/open-terminal:latest \
        -m unittest -v "tests.test_terminal_server.${test#*:}"
done
```

## Fast chat check with compact images

From the checkout root, run the short authenticated chat check against a
built standalone executable. It uses SmolLM2-135M-Instruct Q2_K with one
88 MB weight file and 1,024 context tokens. It checks sign-in enforcement,
model discovery, a completed reply, saved chat creation, and device
placement. Use the full [model workflow](#full-isolated-gpu-workflow) for
the Ministral, long-context, and transfer regression checks.

```bash
python3 python-package/tests/release_handoff.py dist/loaf --chat-only
python3 python-package/tests/release_handoff.py dist/loaf --chat-only --gpu
```

The fixture selects the candidate's bundled `loaf-ollama-cpu` or
`loaf-ollama-cuda13` recipe and uses upstream Open WebUI slim for SQLite
and chat. The candidate runs `loaf build` with those settings. For a
separate test deployment, select the same settings in `config.ini` and
run `loaf build`. A compatible NVIDIA host driver and container runtime
are required for the GPU check.

When the Docker builder can use a cached Ollama 0.40.18 source image, pass
its reference with `--ollama-source`. The image must contain the runtime
files for the selected recipe.

[Model and quantization](https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF) ·
[Ollama build selection](configuration.md#select-an-ollama-build-recipe) ·
[Open WebUI image variants](https://docs.openwebui.com/getting-started/quick-start/#image-variants)

## Check the model example specifiers

This network test requests small public Ollama manifests for the distinct
specifiers in the packaged model INIs and checkout examples.

```bash
RUN_CATALOG_TESTS=1 ./loaf test -v
```

Run all opt-in checks together.

```bash
RUN_DOCKER_TESTS=1 RUN_GPU_TESTS=1 RUN_TERMINAL_SERVER_TESTS=1 RUN_CATALOG_TESTS=1 ./loaf test -v
```

## Check a release candidate

Run the compact handoff against the exact standalone executable you intend
to release. The GitHub and Gitea release jobs run it before uploading
draft assets. It needs a connected Linux amd64 preparation host with a
local rootful Docker daemon and Compose. The destination is a disposable
Debian container with its own Docker Engine, an empty image and volume
store, and no external network. The outer daemon must allow the
destination container to run its nested daemon with `--privileged`.

```bash
python3 python-package/tests/release_handoff.py dist/loaf
```

The check copies a real backup through a read-only mount. The destination
runs the bundled executable's setup, restore, and CPU offline commands.
The HTTP check signs in with the restored account, reads the saved
conversation, and generates a follow-up. It then damages one blob
in the disposable destination, checks that preflight still sees the model,
and confirms that inference fails. The fixture has one chat model and the
three core images.

Use `--keep PATH` to retain the synthetic backup and its test login in a
new directory on a disk-backed filesystem. The rehearsal uses the release
executable's backup compression setting, which defaults to gzip level 4.
Use `--compression LEVEL` to override it with a level from 0 through 9.

```bash
mkdir -p backup
python3 python-package/tests/release_handoff.py dist/loaf \
    --keep backup/release-handoff-fixture
```

[Release workflow](releases.md)

## README test badges

The README badges show each group's latest recorded local date and time
and its pass count. Each badge links to the environment and results.
Run `loaf test --badges` to record the run, generate the SVGs in `docs/badges/`,
and update the README links. Use `-v` to display individual results as usual.
The [GitHub Verify workflow](../.github/workflows/verify.yml) runs
the baseline on Python 3.10, 3.11 and 3.13 and smoke-tests a wheel on each. On
Python 3.13 it runs `loaf setup tests` first, so the provisioner and runner
tests run too, and builds the Debian package and installs it on Debian 12 and
13.

The badge total counts the test cases that executed in that run. For
example, a run that passes four Open Terminal cases shows `4/4 passed` when
the other Open Terminal cases are skipped. Failures count toward the total
and make the badge red. Groups with zero executed cases retain their
previous badge and link. Plain `loaf test` and `loaf test -v` report to the terminal.

| Badge | What counts |
| --- | --- |
| CLI baseline | Core tests with the `RUN_*_TESTS` opt-ins disabled, including CLI, configuration, setup, transfers, and documentation. The local rsync check needs `rsync` on `PATH`. |
| Docker | Compose configuration, image contracts, shell helpers, volume transfer, container lifecycle, and model workflows run on CPU. |
| Open Terminal | The provisioner and runner API tests and the opt-in Open WebUI workspace tests. |
| GPU inference | The real Ollama GPU workflows, including inference and model transfer. |
| Model catalog | One test with subtests for each distinct Hugging Face model reference. |

Run the CLI baseline from the checkout root. Open Terminal API cases depend
on optional packages and count toward the Open Terminal badge if they run.

```bash
RUN_DOCKER_TESTS=0 RUN_GPU_TESTS=0 RUN_TERMINAL_SERVER_TESTS=0 \
    RUN_CPU_TESTS=0 RUN_CATALOG_TESTS=0 ./loaf test --badges
```

Use the following selectors from `python-package/` to count each advanced
group independently. See the prerequisites for [Docker](#docker-configuration-and-real-shell-scripts),
[GPU](#full-isolated-gpu-workflow), [Open Terminal](#terminal-server-checks), and
[catalog](#check-the-model-example-specifiers) above.

```bash
RUN_DOCKER_TESTS=1 ../.venv/bin/python -m unittest -bv \
    tests.test_docker.ComposeContractTests tests.test_docker.BuiltImageTests \
    tests.test_docker.ShellHelperTests tests.test_docker.DockerTests
RUN_DOCKER_TESTS=1 RUN_GPU_TESTS=1 ../.venv/bin/python -m unittest -bv tests.test_docker_gpu
RUN_DOCKER_TESTS=1 RUN_CPU_TESTS=1 ../.venv/bin/python -m unittest -bv tests.test_docker_gpu
RUN_TERMINAL_SERVER_TESTS=1 ../.venv/bin/python -m unittest -bv \
    tests.test_terminal_server.TerminalServerDockerTests
../.venv/bin/python -m unittest -bv \
    tests.test_terminal_server.ProvisionerTests tests.test_terminal_server.RunnerTests
RUN_CATALOG_TESTS=1 ../.venv/bin/python -m unittest -bv tests.test_catalog
```

## Verification coverage

Docker, GPU and network checks are opt-in. The fast suite runs without those
dependencies. Set opt-ins for `loaf test` under `[tests]` in `config.ini`
or override them for a run with the flags below. Transfer tests require Git.

| Group | Coverage |
| --- | --- |
| Launcher and package | Top-level and command help, rejected option combinations and abbreviations, model-list parsing, Compose command order, `run` with its two profiles and the pointer from the former `offline` and `online` commands, `restart` with a profile and without one, where it restarts the profile that serves and refuses when none or both serve or in a dry run, `stop` of one profile that keeps the terminal server while the other one serves, foreground log following, default container removal and its only opt-out `--no-remove-containers`, `--wait-timeout 0` without a limit and refused negative values, `run` refusing `--models-file`, the note for a connected terminal network, dry runs that report a missing backup or a missing standalone executable, the follow-up command that names `--namespace` only when the settings select another one and uses `./loaf` in a checkout with its launcher link or `loaf` in an activated virtualenv, the dry run that refuses a missing terminal key and names `setup terminal-server-key` with `--config-dir`, the pointers from the former setup options and from `link-docker-project`, `package debian` through the release build, default and verbose output, deployment links, failure handling, concurrency, shell quoting, the installed entry points and test output. |
| Bash completion | Runs the generated script in Bash for commands, subcommands, serving profiles, types, guide names, options per command, skipped option values in both `--name value` and `--name=value` form, and directory and file values. Checks that bash-completion finds the per-user file, that install and remove leave foreign files alone, `BASH_COMPLETION_USER_DIR`, and the Debian package case. The Debian package checks complete a word with the system script and confirm that purging removes it. |
| Docker resources | Uses a simulated Docker daemon. Checks that `loaf docker ps` lists only the selected deployment's containers, images, volumes and networks in aligned tables, including the terminal server's chat containers and networks. Checks `--namespace`, chat containers that inherit the provisioner image's Compose labels, the confirmation prompt, `--force` and the pointer from the former `--yes`, the refusal of networks, volumes and images while containers remain, the graceful stop and removal order, a network that disappears after the listing, and that a dry run calls no Docker. Also checks the Compose labels on volumes that loaf creates. |
| Cleanup | Checks both scopes, dry-run, lock contention, symlink boundaries, the question before `--full` removes `install-cache/` with `--force` and without a terminal, the cached Python runtime downloads that do not protect it, and preservation of Git work, credentials and backup archives. |
| Host setup | Checks APT-managed and pyenv interpreters, wheel reuse and refresh, virtualenv recovery, setup diagnostics, the refusal of source setup as root without `--allow-root`, the hint for a virtualenv owned by another user, standalone setup from a wheel installation outside a checkout, the setup parts with `all` as the default and only the named parts otherwise, the refusal of `docs` in a checkout and of `venv` outside one, the default `config.ini` and model list in the selected `--config-dir`, existing settings and keys that stay until `--force` replaces them, a key only with `terminal-server-key`, the virtualenv activation hint, and the NVIDIA toolkit cache. Also checks that Docker setup is opt-in and uses only the prepared APT source when the cache is present. |
| Configuration | Checks packaged defaults and their Compose and Dockerfile fallbacks, local overrides, model file selection, INI validation including the renamed `[launcher] version`, single-line edits that keep comments, and resolved Compose settings. |
| Host integrations | Checks the generated systemd unit, with its Docker dependency and stop timeout like the packaged unit and `--cpu`, and the Continue YAML with its default roles and commented capabilities, against project paths, namespace, WebUI settings and model selection. Checks that the Tether command prints installation steps and the official downloads link. |
| Transfer | Uses real project files and archives with simulated Docker calls. Checks that `loaf.json` comes first and lists every member's size and hash and the launcher's, the restore of every type into a new project, type and alias selection with aliases limited to the archive's types, that a release download folder is no backup folder and an older folder's `SHA256SUMS` moves with it, a launcher with another hash that only warns, recompressed archives that verify and truncated ones that fail, Docker checks before the archive is read, a warning instead of a failure without NVIDIA toolkit packages, the command that resumes serving after a backup, the terminal round trip with the user volume's labels and the key, the Compose labels on restored volumes, restores into the same and another namespace, config links that stay links and links outside their scope that backup and restore refuse, `--verify` running restore's checks without temporary copies, refused populated or mislabeled target volumes before any change, `--no-terminal-server` backups that restore with the terminal disabled and its saved connection removed, single-line settings edits, dry runs that report missing backups, types and checkouts, the cached Python runtime in `installers`, the default settings after restore into a project without them, the guides that standalone setup exported giving way to a restored checkout's `docs/`, the archive and launcher hashes that backup and `--verify` print, the `setup terminal-server-key` hint for a missing key, the `--cpu` hint after a CPU replay, setup and restore inside a backup folder that becomes the project, source restores over existing files with rollback, and the rejection of changed, older, newer and unsafe archives before any change. Also checks Modelfile replay, v1 and v2 model archives, and that the model scripts stream the store and import it without a second copy. The Docker suite merges and mirrors models into another namespace through the CLI. |
| Readiness | Checks Docker and Compose version reporting with the legacy bridge warning only for an enabled terminal server, Open Terminal image and key reporting, the `loaf build` and `loaf restore images` hint for a missing image, GPU and volume probes, the hint for a missing model volume, recognition of the deployment's WebUI port, and relative Ollama v2 manifest links. |
| Open WebUI tool history | Checks the optional compatibility filter for interrupted Ollama tool turns, including provider selection, preserved tool results, and repeated requests. |
| Documentation | Checks local links, heading anchors and guide navigation. Parses Bash examples. Checks that the command reference covers every command and global flag. |
| Deployment | Checks settings layers and `system_config`, system mode and the Debian default, the collision warning, shared locks in setgid directories, the running installation's deployment files for every project, also with a Compose file or a `docker-project` link of its own, settings or a checkout as what makes a directory a project, `loaf version`, and the bundled guides that stay until `--force` replaces them, their exported links and the manual page. |
| Debian package | With Docker, builds the `.deb` with `scripts/build-release.sh --debian-only`, checks control fields, conffiles and files including the standalone executable, then installs, runs and purges it on Debian 12 and 13. The checks include the backup preview with the packaged executable and, on amd64, that executable itself. The manual page passes `groff` with warnings enabled. [Package checks](../python-package/tests/debian_package_checks.sh) |
| Release tagging | Uses temporary Git repositories to check version selection, annotated tags, previews, and tag-only pushes and retries. Requires Python 3.11 or newer. |
| Docker | Checks resolved Compose profiles, the restart policy that brings back the serving services and the terminal server but not the downloader, CPU and GPU device settings, image settings, and that each Dockerfile and build secret stays inside its build context. Tests each export and startup shell scenario, verifies container removal and the stop opt-out, and runs a two-volume archive round trip. `loaf docker rm all` removes exactly the resources of a disposable deployment and keeps the shared image. |
| CPU and GPU model workflows | Check downloaded model exports, offline Ministral inference, startup failure and recovery, explicit Modelfile replay, and model archive mirroring with the connected services running. |
| Terminal server | Checks the WebUI connection preset from the key with `webui_access`, the key secret for both WebUI services and the Compose override that adds it, credential validation, HTTP and WebSocket proxying to the chat's container, the root container policy with only the account's folder of the user volume, network access settings, a runtime APT source that replaces the image's sources and whose replaced file a chat sees on its next start without a new container, runtime scripts at one project path with a revision from the mount paths, `apt install` without `-y` or a terminal, resuming stopped containers and replacing them after a configuration change, startup reconciliation and shutdown, closing the disconnected shells of an idle container before it stops, file browsing and chat deletion, `apt` as root, development tools through Open WebUI, and backup and restore of the image, user volume and key. Transfer tests also cover automatic image inclusion and `--no-terminal-server`. Docker-backed workflows are opt-in. |
| Model catalog | Checks that the example/default model specifiers resolve to Ollama-compatible manifests. |

## Coverage limits

The full CPU and GPU suites download and test Ministral. The compact handoff
tests SmolLM2-135M-Instruct Q2_K, which is smaller than the default
selection. Catalog checks
verify the model references. Hardware fits for the example profiles are
estimates. Native Debian GPU inference, interactive browser login, vision
input, and Tether desktop installation and sign-in are outside direct test
coverage.

Docker logs can contain invalid UTF-8. Regression tests cover strict decoding
for structured data and replacement characters for diagnostic output.
