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

The release executable includes Python. Its setup prepares default
configuration and links the packaged Docker project. Both forms of setup
create the configured terminal server key when it is missing. After source setup,
run `source .venv/bin/activate` or call `./loaf` directly. Source setup
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
`loaf`. Run `loaf link-docker-project` to refresh a link created by an earlier
release. Existing Compose project names, image tags and volume names carry
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

Run `loaf link-docker-project` in the directory where you want to work.
Outside a source checkout, `loaf setup` from a wheel installation prepares a
deployment like the standalone executable: it copies the missing
configuration, links the packaged Docker project and creates the terminal
key. Use the source checkout for host setup and full backups when installing
from a wheel. [Release executable setup](releases.md#run-the-executable)

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

Replace the `loaf` file, then run `./loaf setup` in the deployment. Setup
updates `docker-project` and the exported guides to the new release and keeps
your settings. Build the images while connected with `./loaf build`, or load
newer images from a backup. `./loaf version` shows the release in use.

## Upgrade from the local-inference names

Releases up to 0.4.0 named the default deployment `local-inference`. Its
images were `local/NAME:latest`, its volumes `ollama` and `open-webui`, and
the terminal user volume `loaf-tools-HASH-users`. Every name now starts with
the deployment namespace `loaf`, or `loaf-NAME` for a separate namespace.
Docker cannot rename volumes or images, so the new release starts with empty
volumes until you move the data. [Docker names](#list-and-remove-docker-resources)

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

`loaf` completes its commands, subcommands, archive and resource types,
guide names and options with the Tab key. Option values complete as
directories for `--project-dir`, `--config-dir` and `--backup-dir`, and as
files for `--models-file` and `--archive`.

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
`XDG_DATA_HOME` and `BASH_COMPLETION_USER_DIR` move the per-user directory
as they do for bash-completion.

`install` replaces an earlier loaf script, so run it again after updating a
local installation. Neither command touches a file it did not write. With the
Debian package, `install` reports the system script and writes nothing.
`loaf completion print` writes the script to standard output for another
location.

## Read the bundled guides

Every installation carries the guides of its release. `loaf docs` lists them
and `loaf docs NAME` prints one. Standalone setup writes them to `docs/` in the
deployment, with links into `docker-project/`. A `docs/` directory that setup
did not create stays untouched. The Debian package also installs the manual
page for `man loaf`.

## Link the Docker project

Run this from the directory where you want the link.

```bash
loaf link-docker-project
(cd docker-project && docker compose --profile offline config)
```

From a fresh checkout, you can create the link before setup with
`PYTHONPATH=python-package/src python3 -m loaf link-docker-project`.

`docker-project` is a symlink to the installed package's data directory. It
exposes `compose.yml`, `docker/`, and the [packaged configuration](../python-package/src/loaf/data/config/).
Repeating the command reuses the same link. A link to another loaf release
is updated in place. `loaf` always uses its own deployment files, even
before that update. Any other existing path is reported as a conflict. Use `--dry-run` to preview the link or `--project-dir PATH`
to place it elsewhere.

From a source checkout, `loaf build` and the other launcher commands find the
deployment files automatically. From a wheel installation, use the directory
containing the link for those commands. For manual Compose commands, enter
the link directory. Direct Compose reads its environment and Compose defaults.
Without further settings, it uses the default deployment `loaf`, and Open
WebUI starts without the terminal connection that `loaf` presets. For a
separate namespace, set `LOAF_NAMESPACE` and the matching project name, for
example `LOAF_NAMESPACE=v1-2-3 docker compose --project-name loaf-v1-2-3`.
[Configuration overrides](configuration.md#overrides)

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

From the project directory on Debian, run setup with system Python.

```bash
PYTHONPATH=python-package/src python3 -m loaf setup
```

The command prepares the virtualenv, Python wheel cache, NVIDIA toolkit
packages and terminal key under the checkout lock. `install` runs the same
preparation. Run it as the checkout's owner: the APT steps use `sudo`. As
root, setup refuses a checkout that another user owns, because root would own
the virtualenv and caches and link them to its own Python. Add `--allow-root`
to prepare the checkout as root anyway. If an earlier root setup left a
`.venv` that your user cannot replace, setup names the `sudo rm -rf` command
that removes it. Use `setup --terminal-server-key` to prepare only the key. Use
`setup --dry-run` to preview it. Once installed, run `build` or `upgrade`, then
`download`, `offline` to check the deployment, and `backup` to archive it.
[Host prerequisites](debian.md) · [Quick Setup](../README.md#quick-setup) ·
[Failure guide](../README.md#if-a-command-fails)

## Modes

Build the images and download the selected models before serving. To refresh
the upstream images, use `loaf upgrade` and restart the serving profile to
apply the rebuild. [Command reference](cli-help.md#commands)

```bash
loaf build
loaf download --models-file config/model-examples/models.16gb.ini
loaf status
loaf offline
```

The downloader pulls each selected model. Both serving modes apply saved
`.model` files under `/ollama/config` at backend startup. To apply manually
edited definitions, run `loaf stop` and then start the desired serving mode.
Removing a model from the selection leaves its weights, manifests and
Modelfiles in the store.

`online` and `offline` also start the provisioner when the terminal server is
enabled. The provisioner uses the Compose profile `terminal` and service
`terminal-server`. Serving and status commands check the configured bearer
key file when the feature is enabled. [Terminal server setup](terminal-server.md#enable-and-connect)
[Upgrade an existing terminal deployment](terminal-server.md#upgrade-an-existing-terminal-deployment)

### Serve on CPU

Both serving commands use NVIDIA GPU acceleration by default. Add `--cpu`
when preparing or testing the deployment on a machine without GPU access.

```bash
loaf status --cpu
loaf offline --cpu
```

Use `loaf online --cpu` when you want the connected serving profile. CPU
serving uses the same images, volumes, model imports, and optional terminal
server. CPU inference uses system RAM and can take longer than GPU inference.
Run `loaf backup` as usual and start the destination with `loaf offline` for
NVIDIA inference.

### Stop and remove containers

Run `loaf stop` to stop the selected deployment and remove its Compose service
containers. The command keeps named volumes, bind-mounted files, images
and networks. It removes container logs and files stored only in a container's
writable layer. Use `loaf stop --no-remove-containers` to keep stopped
containers and their logs. Preview either form with `--dry-run`.
To remove networks, volumes or images as well, use
[`loaf docker rm`](#list-and-remove-docker-resources).
[Compose container removal](https://docs.docker.com/reference/cli/docker/compose/rm/)

### List and remove Docker resources

Every Docker resource of a deployment starts with its namespace: `loaf` for
the default deployment, or `loaf-NAME` with `--namespace NAME`. `loaf docker ps`
lists the selected deployment's containers, images, volumes and networks.

```bash
loaf docker ps
loaf docker rm containers
loaf docker rm all
```

| Type | Names in the default deployment |
| --- | --- |
| Containers | Compose services such as `loaf-open-webui-1`, and the terminal server's chat containers `loaf-terminal-KEY`. |
| Images | `loaf/ollama-online`, `loaf/ollama-offline`, `loaf/open-webui` and `loaf/open-terminal`, tagged `latest` or the version. |
| Volumes | `loaf-ollama` holds the models, `loaf-open-webui` the Open WebUI accounts and chats, and `loaf-terminal-users` the terminal user folders. |
| Networks | Compose networks such as `loaf_inference`, and the terminal server's chat networks `loaf-terminal-KEY`. |

`loaf docker rm` takes one or more of `all`, `containers`, `images`,
`volumes` and `networks`. It lists the resources and asks for confirmation.
Add `--yes` to skip the question, for example in a script. Without a
terminal, `rm` requires `--yes`. Removal stops the running services within
their grace periods. The terminal server then stops its chat containers.
Next, `rm` removes the containers, networks, volumes and images in that
order. Networks, volumes and images can go only once the deployment has no
containers. In that case, `rm` refuses and prints the command that includes
`containers`. `--dry-run` describes the removal without Docker. Removing an
image tag that another deployment also uses keeps the image for that
deployment.

Removed volumes cannot be recovered. Save them first with
`loaf backup volumes`. An offline host cannot rebuild removed images; they
come back with `loaf restore images`. Base images, build cache and other
projects' resources are not affected.

### Check readiness with status

Run `loaf status` to check serving readiness. The report opens with the deployment
name, project directory and configured WebUI URL. Configuration covers the
launcher settings and resolved Compose configuration. Local images and GPU
covers image presence and NVIDIA access from a disposable container. Storage
and networking covers volume readability, the presence of local model data
(including Ollama's `manifests-v2` catalog), and the configured WebUI host ports.
If Docker cannot verify GPU access, the report points to `loaf status --cpu`.
That command checks CPU serving readiness and skips the NVIDIA probe.

Compose 2.26.1 through 2.29.x produces a warning and uses the NVIDIA device
reservation for GPU access. Versions below 2.26.1 fail the version check.
Docker Engine 26 and 27 produce a warning about Open Terminal's legacy
bridge isolation. When Open Terminal is enabled, Configuration also checks
its bearer key. Its image appears with the other local images. Image checks
show `OK` when the image exists locally or `FAIL` when it is missing. The
container list shows state and health.

A running WebUI from the selected deployment can own its configured port.
A missing WebUI volume can be created on first startup. Host port checks
require a local Docker endpoint. The final line summarizes whether preflight
passed. A failure produces a nonzero exit status.
[Verification guide](verification.md#debian-smoke-checks)

## Options

Use `loaf COMMAND --help` to look up a flag. To see the exact Compose
commands that would run for the 8 GB selection, preview it with `-v`.
[Command and flag reference](cli-help.md)

```bash
loaf -v download --models-file config/model-examples/models.8gb.ini --dry-run
```

During a run, `loaf` reports the major steps and keeps Docker and Ollama's
progress visible. While service startup waits for readiness, Compose's
terminal display shows each service's elapsed time. In logs and other output
that is not a terminal, `loaf` reports the elapsed time every 10 seconds. Transfers report bytes and throughput. Add `-v` to a
dry run to see the planned Compose commands.

Use `loaf offline --attach` or `loaf -a status` to follow logs. The flag is
available for Compose commands and full backup and restore. Commands that start
services follow those services. Other commands follow the selected deployment.
Press **Ctrl+C** to leave the log view with services still running. `loaf stop`
can run from another terminal.
[Docker Compose logs](https://docs.docker.com/reference/cli/docker/compose/logs/)

The startup command printed after a download retains the selected project,
override directory and namespace. [Configuration guide](configuration.md#project-configuration)

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
loaf --namespace v1-2-3 offline
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

`loaf clean` removes loaf's runtime cache under `~/.local/share/loaf/`,
or `$XDG_DATA_HOME/loaf/` when configured. It also clears recognized
temporary files and Python bytecode from the selected project.

`--full` also clears the project virtualenv, generated build and release
files, and the packaged Docker link. It asks before it removes
`install-cache/`, because an offline host cannot download those installers
again. Without a terminal it keeps the cache; add `--yes` to remove it
without asking. Both modes preserve
Git history and working files, saved backups, configuration, credentials,
and Docker data. A generated directory that contains Git work or a
backup archive is kept. Lock files stay in place so commands can coordinate
cleanup with active work.

If cleanup reports an invalid project configuration, correct it and retry.
Cleanup needs to resolve the configured terminal key path before deleting
anything, including a key stored inside a generated directory.

Use the original standalone executable to run setup again after a full
clean. In a source checkout, run the source setup command to recreate
the virtualenv. [Quick Setup](../README.md#quick-setup)

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
definitions/blobs, then retry `import` and start WebUI with `offline`.

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
loaf link-docker-project
(cd docker-project && docker compose --profile download exec ollama-online export-modelfiles)
```

Re-export after model updates. Removing an old export prevents its startup
re-import; it does not delete an already registered model.
