# Verification guide

[Project overview](../README.md)

Run the tests in Debian or Debian in WSL2, from the project directory with the
[editable package installed in `.venv`](launcher.md#install-the-package).
The suite uses Python's standard `unittest` runner.
See the [verification record](verification-record.md) for dated results.

## Run the fast suite

```bash
.venv/bin/loaf test
.venv/bin/loaf test -v
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

## README test badges

The README badges show each group's latest recorded local date and time
and its pass count. Each badge links to the environment and results.
Run `loaf test --badges` to record the run, generate the SVGs in `docs/badges/`,
and update the README links. Use `-v` to display individual results as usual.
The [GitHub Verify workflow](../.github/workflows/verify.yml) runs
the baseline on Python 3.10 and 3.13 and smoke-tests a wheel.

The badge total counts the test cases that executed in that run. For
example, a run that passes four Open Terminal cases shows `4/4 passed` when
the other Open Terminal cases are skipped. Failures count toward the total
and make the badge red. Groups with zero executed cases retain their
previous badge and link. Plain `loaf test` and `loaf test -v` report to the terminal.

| Badge | What counts |
| --- | --- |
| CLI baseline | Core tests with the four `RUN_*_TESTS` opt-ins disabled, including CLI, configuration, setup, transfers, and documentation. The local rsync check needs `rsync` on `PATH`. |
| Docker | Compose configuration, image contracts, shell helpers, volume transfer and container lifecycle. |
| Open Terminal | The provisioner and runner API tests and the opt-in Open WebUI workspace tests. |
| GPU inference | The real Ollama GPU workflows, including inference and model transfer. |
| Model catalog | One test with subtests for each distinct Hugging Face model reference. |

Run the CLI baseline from the checkout root. Open Terminal API cases depend
on optional packages and count toward the Open Terminal badge if they run.

```bash
RUN_DOCKER_TESTS=0 RUN_GPU_TESTS=0 RUN_TERMINAL_SERVER_TESTS=0 \
    RUN_CATALOG_TESTS=0 .venv/bin/loaf test --badges
```

Use the following selectors from `python-package/` to count each advanced
group independently. See the prerequisites for [Docker](#docker-configuration-and-real-shell-scripts),
[GPU](#full-isolated-gpu-workflow), [Open Terminal](#terminal-server-checks), and
[catalog](#check-the-model-example-specifiers) below.

```bash
RUN_DOCKER_TESTS=1 ../.venv/bin/python -m unittest -bv \
    tests.test_docker.ComposeContractTests tests.test_docker.BuiltImageTests \
    tests.test_docker.ShellHelperTests tests.test_docker.DockerTests
RUN_DOCKER_TESTS=1 RUN_GPU_TESTS=1 ../.venv/bin/python -m unittest -bv tests.test_docker_gpu
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
| Launcher and package | Top-level and command help, model-list parsing, Compose command order, foreground log following, default container removal and its opt-out, default and verbose output, deployment links, failure handling, concurrency, shell quoting, the installed entry points and test output. |
| Cleanup | Checks both scopes, dry-run, lock contention, symlink boundaries, and preservation of Git work, credentials and backup archives. |
| Host setup | Checks APT-managed and pyenv interpreters, wheel reuse and refresh, virtualenv recovery, setup diagnostics, and the NVIDIA toolkit cache. Also checks that Docker setup is opt-in and uses only the prepared APT source when the cache is present. |
| Configuration | Checks packaged defaults, local overrides, model file selection, INI validation, and resolved Compose settings. |
| Host integrations | Checks the generated systemd unit and Continue YAML against project paths, deployment version, WebUI settings and model selection. Checks that the Tether command prints installation steps and the official downloads link. |
| Transfer | Uses real project files and archives with simulated Docker calls. Checks the standalone handoff folder, path and payload verification, minimal setup, recovery of saved configuration and cached installers, and image and volume transfer. Also checks Modelfile replay and v2 model archives. With rsync, checks the model archive merge and deletion scripts. |
| Readiness | Checks Docker and Compose version reporting, Open Terminal image and key reporting, GPU and volume probes, recognition of the deployment's WebUI port, and relative Ollama v2 manifest links. |
| Open WebUI tool history | Checks the optional compatibility filter for interrupted Ollama tool turns, including provider selection, preserved tool results, and repeated requests. |
| Documentation | Checks local links, heading anchors and guide navigation. Parses Bash examples. |
| Release tagging | Uses temporary Git repositories to check version selection, annotated tags, previews, and tag-only pushes and retries. Requires Python 3.11 or newer. |
| Docker | Checks resolved Compose profiles, CPU and GPU device settings, and image settings. Tests each export and startup shell scenario, verifies container removal and the stop opt-out, and runs a two-volume archive round trip. |
| GPU | Checks downloaded model exports, offline Ministral inference, startup failure and recovery, explicit Modelfile replay, and model archive mirroring with the connected services running. |
| Terminal server | Checks credential validation, HTTP and WebSocket proxying, account and chat storage, file browsing and chat deletion, development tools through Open WebUI, and backup and restore of the image, user volumes and key. Transfer tests also cover automatic image inclusion and `--no-terminal-server`. Docker-backed workflows are opt-in. |
| Model catalog | Checks that the example/default model specifiers resolve to Ollama-compatible manifests. |

## Debian smoke checks

Run these checks after [starting the offline stack on Debian](../README.md#quick-setup).
Use a login with [Docker access](debian.md#docker-access). These commands
use the unversioned deployment and default port.
For a configured deployment, `loaf status` checks serving prerequisites and
lists its containers. `loaf offline` prints its browser address.

```bash
.venv/bin/loaf link-docker-project
(
    cd docker-project
    docker compose --profile offline ps
    docker compose --profile offline exec ollama-offline nvidia-smi
    docker compose --profile offline exec ollama-offline ollama list
)
curl -fsS http://localhost:3000/health
```

After sending a message, inspect the active model.

```bash
(cd docker-project && docker compose --profile offline exec ollama-offline ollama ps)
```

Look for `100% GPU`. An empty list means no model is currently loaded.

On a host without NVIDIA GPU access, run `loaf status --cpu` and
`loaf offline --cpu` or `loaf online --cpu`. Check WebUI health and send a chat
message. Ollama's process list should report `100% CPU` while the model is
loaded. [CPU serving](launcher.md#serve-on-cpu)

## Docker configuration and real shell scripts

Build the images while connected, then enable the Docker checks.

```bash
.venv/bin/loaf build
RUN_DOCKER_TESTS=1 .venv/bin/loaf test -v
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
[Shell fixtures](../python-package/tests/script_checks.sh) · [Volume fixtures](../python-package/tests/archive_checks.sh)

Select one case by name from the package directory. Use `-b` with direct
`unittest` runs to buffer test output as `loaf test` does.

```bash
(cd python-package && RUN_DOCKER_TESTS=1 ../.venv/bin/python -m unittest -bv \
    tests.test_docker.DockerTests.test_stop_removes_container_and_preserves_its_named_volume)
```

## Full isolated GPU workflow

Use the built local images, working NVIDIA container access, internet access,
and disk space for the model and staged copies in both Docker and the host's
temporary directory. Set `TMPDIR` to a disk-backed directory with enough
space if `/tmp` is a small memory-backed filesystem. The suite downloads
Ministral and exports its Modelfiles once. Each serving workflow starts from
a copy in its own volume and Compose project.

```bash
RUN_DOCKER_TESTS=1 RUN_GPU_TESTS=1 .venv/bin/loaf test -v
```

The individual cases check the source and alias Modelfiles, recovery of a
missing manifest, explicit replay of a new definition, and recovery from an
invalid definition that prevents WebUI startup. The inference case checks
the 16,384-token default and full GPU placement. The transfer case switches
to the connected profile, checks WebUI health on a temporary loopback port,
and mirrors the model store with `--delete`. It verifies that the added
alias disappears and both connected containers keep their IDs.
Cleanup targets the generated projects and their disposable resources.
[GPU cases](../python-package/tests/test_docker_gpu.py)

## Terminal server checks

The optional [terminal server Docker checks](terminal-server.md#acceptance-checks) use
`RUN_TERMINAL_SERVER_TESTS=1`, Docker Engine 26+, and locally built
`local/open-webui:latest` and `local/open-terminal:latest` images from the current
checkout. Rebuild the terminal image after changes to the provisioner.
Run it on native Debian and Debian in WSL2 with the same local images
before and after host disconnection. The serving cases drive
Open WebUI's terminal proxy with two authorized accounts and a denied
account, exercise GCC/G++, Python, and Sphinx workflows, and repeat the
builds after a cold restart on the internal control network. The online and
offline cases have separate Docker projects and volumes. They need no
model or GPU.

The migration case checks that an older provisioner blocks startup until
the launcher stops it and that an unrelated provisioner keeps running.
The serving cases exercise saved connections using the old hostname.
They check the default and recent entries offered in the working-directory
menu, selection before the first message, and visibility of files created by
model commands.
They also check chat isolation and volume removal after chat deletion.
The suite includes a Docker backup round trip for a versioned terminal image,
a workspace file with its ownership, and an owner-only bearer key.

The serving cases exercise production isolation by default and use `iproute2` on
the local Docker host to inspect the bridge. It checks that the host bridge
has no IPv4 address and each workspace has no default route, IPv6 address,
or connection to the other workspace or control API. On Engine 26 and 27,
`[tests] allow_legacy_terminal_bridge = true` runs the workflows with ordinary
internal workspace bridges for comparison. Set
`ALLOW_LEGACY_TERMINAL_BRIDGE=1` for the same test-only override from the shell.

Local HTTP and WebSocket proxy tests require `aiohttp`. The local
command-timeout test requires the Open Terminal Python package pinned
in the terminal image and `httpx`.

## Check the model example specifiers

This network test requests small public Ollama manifests for the distinct
specifiers in the packaged `models.ini` and example INIs.

```bash
RUN_CATALOG_TESTS=1 .venv/bin/loaf test -v
```

Run all opt-in checks together.

```bash
RUN_DOCKER_TESTS=1 RUN_GPU_TESTS=1 RUN_TERMINAL_SERVER_TESTS=1 RUN_CATALOG_TESTS=1 .venv/bin/loaf test -v
```

## Coverage limits

The GPU suite downloads and tests Ministral. Catalog checks cover the other
example specifiers, whose hardware fits are estimates. Native Debian GPU
inference, the 8 GB hardware profile, interactive browser login, vision input,
and Tether desktop installation and sign-in are outside direct test coverage.

Docker logs can contain invalid UTF-8. Regression tests cover strict decoding
for structured data and replacement characters for diagnostic output.
