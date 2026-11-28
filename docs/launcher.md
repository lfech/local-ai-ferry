# Launcher and model selection

[Project overview](../README.md)

Run commands from the project directory unless a step says otherwise.

## Install the package

The installable project is [python-package](../python-package/pyproject.toml),
with Python source under `python-package/src/loaf`. Its distribution name is
`local-ai-ferry`, and its console command is `loaf`. The package ships
the Compose project and configuration defaults. The test suite lives in
`python-package/tests/` in the checkout.

```bash
PYTHONPATH=python-package/src python3 -m loaf install
./loaf --help
```

The release executable includes Python. [What setup prepares](cli-help.md#setup)
After source setup, run `source .venv/bin/activate` or call `./loaf` directly. Source setup
links `./loaf` to `.venv/bin/loaf` unless that path exists, so a checkout uses
the same command as standalone and restored deployments.
[Release executable](releases.md#run-the-executable)

The runtime has no third-party dependencies. Setuptools is a build dependency.
An [editable pip install](https://pip.pypa.io/en/stable/topics/local-project-installs/#editable-installs)
links to the source directory; reinstall after moving the checkout or changing
package metadata. The `loaf` command and `python -m loaf` call the same launcher,
registered through a [console entry point](https://setuptools.pypa.io/en/latest/userguide/entry_point.html#console-scripts).

`loaf --help` lists commands and global flags. Use `loaf COMMAND --help` for
command flags. On Debian, use `./loaf` with a login that has
Docker access. [Command reference](cli-help.md) · [Docker access](debian.md#docker-access)

## Prepare Python installation offline

On Debian, run `loaf setup` or `loaf install` to prepare the launcher wheel and
its declared build and runtime requirements in `install-cache/wheels/`. The
bootstrap, setup and backup reuse the cache when it matches the checkout and
Python environment. Setup tries local wheels first and fetches missing
requirements through pip's configured sources. Older caches without a cache
record are refreshed once.

Setup installs the checkout in editable mode from the local wheel directory.
Repeat setup reuses a working editable installation with current package
metadata. Python preparation prints short status messages and shows pip's
output when the step fails. Backup bundles the wheels and their cache record
for recovery. [Backup and transfer](transfer.md)

After updating an older checkout, run the source setup command to install
`loaf`. A `docker-project` link from an earlier release is no longer used;
`loaf clean --full` removes it. Existing Compose project names, image tags and volume names carry
over to the new launcher.

After recovering a Debian checkout, run the same bootstrap command. A
complete wheel directory for the selected Python lets pip work offline.

```bash
PYTHONPATH=python-package/src python3 -m loaf install
./loaf --help
```

The launcher and its build backend use pure-Python wheels. The destination
Python must meet the packages' version requirements. To install the recovered
launcher wheel into an existing virtualenv, activate that environment and run:

```bash
python -m pip install --no-index --find-links install-cache/wheels local-ai-ferry
```

Outside a source checkout, `loaf setup` from a wheel installation prepares a
deployment like the standalone executable. The release wheel carries the
standalone executable that backups include; a wheel built from the checkout
does not. Use the source checkout for host setup when installing from a wheel.
[Release executable setup](releases.md#run-the-executable)

### Test packages

The terminal server API tests need `aiohttp`, `httpx` and Open Terminal.
Run `loaf setup tests` with the virtualenv that runs `loaf test`. It
requires Python 3.11 or newer and a source checkout. It caches the pinned
packages from
[`tests/requirements.txt`](../python-package/tests/requirements.txt) and
their requirements as wheels in `install-cache/wheels/`, with a separate
cache record. Then it installs them from that directory only.

```bash
./loaf setup tests
```

Repeat runs reuse the cache when the pins and Python are unchanged. Setup
tries local wheels first and fetches missing packages through pip's
configured sources. Backups from the checkout carry the test wheels and
their record. A recovered checkout with the same Python version and
platform can install them offline. Some wheels are platform-specific.
`loaf clean --full` asks before it removes them with the rest of the install cache.
[pip local install reference](https://pip.pypa.io/en/stable/cli/pip_install/#examples)

## Upgrade the standalone executable

Replace the `loaf` file, then run `./loaf setup docs --force` in the
deployment to replace the exported guides with those of the new release.
Setup keeps your settings, and the new `loaf` brings its own deployment files.
Build the images while connected with `./loaf build`, or load newer images
from a backup. `./loaf version` shows the release in use.

## Upgrade from the local-inference names

Releases up to 0.4.0 named the default deployment `local-inference`. Its
images were `local/NAME:latest`, its volumes `ollama` and `open-webui`, and
the terminal user volume `loaf-tools-HASH-users`. Every name now starts with
the deployment namespace `loaf`, or `loaf-NAME` for a separate namespace.
Docker cannot rename volumes or images, so the new release starts with empty
volumes until you move the data. [Docker names](cli-help.md#docker)

1. Before you replace `loaf`, save the volumes with the old release and stop
   the old deployment:

   ```bash
   loaf backup volumes
   loaf stop
   ```

2. Restore the volume types with the new release, which creates the new
   volumes. Omit `terminal` if the backup has no terminal type. A backup
   from the old release cannot restore `images`, because its image tags use
   the old names.

   ```bash
   loaf restore models webui terminal
   ```

3. Build the images while connected with `loaf build`, or tag the existing
   images with the new names on an offline host. Skip `open-terminal` if the
   terminal server is not in use.

   ```bash
   for image in ollama-online ollama-offline open-webui open-terminal; do
       docker image tag "local/$image:latest" "loaf/$image:latest"
   done
   ```

4. Start the deployment and check your models, accounts and terminal files.
   Then remove the old chat containers, networks, volumes and image tags.
   `loaf docker` covers only the new names. Images tagged in step 3 keep
   their new tags.

   ```bash
   old=label=loaf.tool-server.project=local-inference
   docker container ls -aq --filter "$old" | xargs -r docker container rm
   docker network ls -q --filter "$old" | xargs -r docker network rm
   docker network ls -q --filter label=com.docker.compose.project=local-inference | xargs -r docker network rm
   docker volume ls -q --filter "$old" | xargs -r docker volume rm
   docker volume rm ollama open-webui
   docker image rm local/ollama-online:latest local/ollama-offline:latest local/open-webui:latest local/open-terminal:latest
   ```

Without the old release or its images, copy the volumes directly instead of
steps 1 and 2. Build the new images first, keep the old deployment stopped,
and replace `HASH` with the one in the old terminal volume's name. Each target
volume must be new; it gets the labels that Compose and the terminal server
expect.

```bash
loaf build
copy_volume() {
    docker volume create "${@:3}" "$2"
    docker run --rm --network none --mount "source=$1,target=/from,readonly" --mount "source=$2,target=/to" \
        --entrypoint cp loaf/ollama-online:latest -a /from/. /to/
}
copy_volume ollama loaf-ollama --label com.docker.compose.project=loaf --label com.docker.compose.volume=ollama
copy_volume open-webui loaf-open-webui --label com.docker.compose.project=loaf --label com.docker.compose.volume=open-webui
copy_volume loaf-tools-HASH-users loaf-terminal-users --label loaf.terminal.project=loaf --label loaf.terminal.users=true
```

For a deployment that used `--version VERSION`, add `--namespace VERSION` to
the new loaf commands, and rename `[launcher] version` to `[launcher] namespace`
in its `config.ini`. The old names were `VERSION-local-inference` for the
project and volume prefix and `local/IMAGE:VERSION-local-inference` for the
images.

## Bash completion

| Setup | Install | Remove |
| --- | --- | --- |
| Local: source checkout, standalone executable or wheel | `loaf completion install` writes the script for the current user to `~/.local/share/bash-completion/completions/loaf`. | `loaf completion remove` |
| System: Debian package | The package installs `/usr/share/bash-completion/completions/loaf`. | Removing the package removes it. |

```bash
loaf completion install
loaf completion remove
```

Open a new shell after either command. bash-completion loads the script the
first time you press Tab after `loaf` or `./loaf`. Debian's default
`~/.bashrc` enables bash-completion; install the Debian package
`bash-completion` if it is missing. Without it, add
`source ~/.local/share/bash-completion/completions/loaf` to `~/.bashrc`.

Run `install` again after updating a local installation.
[completion commands](cli-help.md#completion)

## Inspect the containers

`loaf` runs Docker Compose with the deployment files of its own installation:
the source checkout, the standalone executable's runtime, the wheel or the
Debian package. `loaf version` names their directory. A project holds only
settings and data, so there is no Compose project to enter. Every container
has a fixed name, `NAMESPACE-SERVICE-1`, which `loaf docker ps` lists, and
plain Docker commands reach it by that name.

```bash
loaf docker ps
docker logs --tail 100 loaf-ollama-offline-1
docker exec loaf-ollama-offline-1 ollama ps
```

In the namespace `loaf-v1-2-3`, the same container is `loaf-v1-2-3-ollama-offline-1`.
`loaf -v run offline --dry-run` prints the complete Compose commands with
their settings. [Container names](cli-help.md#docker)

## Select models

The editable [config/models.ini](../config/models.ini) starts with the bundled
model selection. Its `[models.default]` entry applies when the file contains
no named sections. Define one section per model.

```ini
[models.chat]
name = Qwen3-4B-Chat
model = hf.co/Qwen/Qwen3-4B-GGUF:Q5_K_M
```

The optional `name` sets the name that Ollama and WebUI show. Use
letters, digits, dots, underscores or hyphens, plus an optional tag such as
`:latest`. Download pulls the source, creates the name and removes the long
`hf.co/...` name, so WebUI lists the model once. The model data stays with the
new name. To keep the long name too, add another entry for the same `model`
without `name`. Export saves the Modelfile of each selected name for offline
import. Run download again after adding a `name` to an already cached model.

Set `[launcher] models_file` in a [local `config.ini`](configuration.md#project-configuration) for a persistent selection,
or pass `--models-file PATH` for one run. The selected INI supplies the entire
selection. Entries use `hf.co/owner/repository[:tag]`, where the tag can be a
quantization or an exact GGUF filename. Invalid or empty selections fail
before Docker starts. [Example selections](models.md)

## Quick Setup

The [README's Quick Setup](../README.md#quick-setup) lists the steps. Source
setup runs under the checkout lock; run it as the checkout's owner, because
the APT steps use `sudo`. [What setup prepares](cli-help.md#setup) ·
[Host prerequisites](debian.md)

## Modes

Build the images and download the selected models before serving. To refresh
the upstream images, use `loaf upgrade` and restart the serving profile to
apply the rebuild. [Command reference](cli-help.md#commands)

```bash
loaf build
loaf download --models-file config/model-examples/models.16gb.ini
loaf status
loaf run offline
```

The downloader pulls each selected model. Both serving modes apply saved
`.model` files under `/ollama/config` at backend startup. To apply manually
edited definitions, run `loaf restart`, which stops the serving profile and
starts it with new containers.
Removing a model from the selection leaves its weights, manifests and
Modelfiles in the store.

`loaf run` also starts the provisioner when the terminal server is
enabled. The provisioner uses the Compose profile `terminal` and service
`terminal-server`. [Terminal server setup](terminal-server.md#enable-and-connect)
[Upgrade an existing terminal deployment](terminal-server.md#upgrade-an-existing-terminal-deployment)

### Serve on CPU

Both serving commands use NVIDIA GPU acceleration by default. Add `--cpu`
when preparing or testing the deployment on a machine without GPU access.

```bash
loaf status --cpu
loaf run offline --cpu
```

Use `loaf run online --cpu` when you want the connected serving profile. CPU
serving uses the same images, volumes, model imports, and optional terminal
server. CPU inference uses system RAM and can take longer than GPU inference.
Run `loaf backup` as usual and start the destination with `loaf run offline` for
NVIDIA inference.

### Stop and inspect the deployment

[`loaf stop`](cli-help.md#stop) stops the deployment, [`loaf restart`](cli-help.md#restart)
starts a profile again with new containers, [`loaf status`](cli-help.md#status)
checks its readiness, and [`loaf docker`](cli-help.md#docker) lists or removes
its containers, images, volumes and networks.

## Options

To see the exact Compose commands that would run for the 8 GB selection,
preview it with `-v`. [Shared options](cli-help.md#shared-options)

```bash
loaf -v download --models-file config/model-examples/models.8gb.ini --dry-run
```

## Run a separate namespace

`--namespace NAME` runs a separate deployment in the Docker namespace
`loaf-NAME`. Pass the same name to each command that operates on the
deployment. Use a lowercase identifier starting with a letter or digit. The
remaining characters can include lowercase letters, digits, hyphens and
underscores. A release number works well, for example `v1-2-3` for 1.2.3.

```bash
loaf --namespace v1-2-3 build
loaf --namespace v1-2-3 download
loaf --namespace v1-2-3 backup models
loaf --namespace v1-2-3 run offline
```

This uses Compose project `loaf-v1-2-3`, image tags such as
`loaf/ollama-offline:v1-2-3`, and volumes such as `loaf-v1-2-3-ollama`.
Set `[launcher]` `namespace` in `config.ini` to select it by default, and
`--namespace=` to select the default namespace `loaf` for one command. The
default namespace has images tagged `latest`, so `latest` is not available as
a name. Each serving deployment needs its own host port if you run them
together. Set `[webui]` `port` in each checkout as needed.
[Transfers between namespaces](transfer.md#transfers-between-namespaces)

## Clean generated files

```bash
loaf clean
loaf clean --full --dry-run
loaf clean --full
```

[What clean removes and keeps](cli-help.md#clean)

## Run tests

See the [verification guide](verification.md) for `loaf test` options,
integration prerequisites and manual smoke checks.

## Failure and concurrency behavior

The model list is validated before Docker runs. Serving and preparation
commands validate Compose and stop conflicting services before starting a
profile. Build and upgrade run separately from mode switches.

A project lock in `.run.lock` rejects overlapping launcher runs in this checkout.
The file remains after completion; the OS lock is released on exit. It does not
control manual Docker commands or another checkout sharing the named volumes.

A failed command ends the workflow with a nonzero status. Completed downloads
and exports remain. A failed pull can leave the downloader running, so resume the
preparation or explicitly run offline mode. An interrupted launcher returns
status 130. There is no automatic deletion or rollback of model data.

If a definition fails during backend startup, import returns a nonzero status
and WebUI stays stopped. Earlier successful registrations can remain. Fix the
definitions/blobs, then retry the import and start WebUI with `loaf run offline`.

## Export details

The helper runs `ollama show --modelfile <model>`. It writes through a temporary
file and replaces an export only after success. A failed export preserves its
previous definition. Namespace directories and tags are retained, for example:

```text
/ollama/config/hf.co/mistralai/Ministral-3-3B-Reasoning-2512-GGUF:Q4_K_M.model
```

Offline startup derives the model name from the path relative to
`/ollama/config`, without `.model`. Exports may contain multiple `FROM` paths
for language weights and vision projectors. Keep the same `/ollama` mount path
on both hosts and transfer the model blobs/manifests along with these files.

To export all installed models manually while the downloader is running:

```bash
docker exec loaf-ollama-online-1 export-modelfiles
```

Re-export after model updates. Removing an old export prevents its startup
re-import; it does not delete an already registered model.
