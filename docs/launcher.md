# Serving and model selection

[Project overview](../README.md)

Run commands from the project directory unless a step says otherwise.

## Quick Setup

The [README's Quick Setup](../README.md#quick-setup) lists the steps.

[Installation](installation.md) · [Host prerequisites](debian.md)

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
before Docker starts.

[Example selections](models.md)

## Modes

Build the images and download the selected models before serving. To refresh
the upstream images, use `loaf upgrade` and restart the serving profile to
apply the rebuild.

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
`terminal-server`.

[Command reference](cli-help.md#commands) ·
[Terminal server setup](terminal-server.md#enable-and-connect) ·
[Upgrade an existing terminal deployment](upgrade.md#upgrade-an-existing-terminal-deployment)

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
their settings.

[Container names](cli-help.md#docker)

## Options

To see the exact Compose commands that would run for the 8 GB selection,
preview it with `-v`.

```bash
loaf -v download --models-file config/model-examples/models.8gb.ini --dry-run
```

[Shared options](cli-help.md#shared-options)

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
