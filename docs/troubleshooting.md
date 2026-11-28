# Troubleshooting

[Project overview](../README.md)

Run commands from the project directory unless a step says otherwise.
Create the [`docker-project` link](launcher.md#link-the-docker-project) before
running the direct Compose examples.

## Context limits

If `hello :)` gives a message such as `7008 tokens exceeds 4096`, the whole
request is larger than the active context. System instructions, tools, history
and attachments count too.

Set the model/chat's `num_ctx` to `16384` in WebUI, or clear a small override
when using a model with no larger preset. Check [context precedence](configuration.md#webui-settings).
The Qwen3 examples need an explicit 16384 setting to override their 40960 preset.

Inspect the loaded model after making a request:

```bash
(cd docker-project && docker compose --profile offline exec ollama-offline ollama ps)
```

The `CONTEXT` column shows the active context. A larger value uses more memory.

## Reasoning without a final answer

Thinking consumes output tokens too. A small maximum can stop a reasoning model
before its final answer. Start with 4096 output tokens and the recommended
sampling values in [WebUI settings](configuration.md#webui-settings). A test at
zero temperature with only 1024 output tokens hit that limit; subsequent tests
with temperature 0.7 and a larger allowance completed.

## Conversation roles must alternate

Ollama renders chats using the selected model's template. Some older GGUF
templates, including the cached Ministral 3 3B Reasoning template, require an
assistant reply before the next user turn. A reply can end on a tool result
when Open WebUI stops it to answer a queued follow-up. The strict template
then rejects a new user message before inference. The same failure has been
reported with larger models.
[Open WebUI message queue](https://docs.openwebui.com/features/chat-conversations/chat-features/message-queue/) ·
[Mistral Vibe issue](https://github.com/mistralai/mistral-vibe/issues/1006)

Use the optional Ollama Tool History Compatibility filter for an affected
model. It adds an empty assistant turn to the request when a user follows
tool results from an unfinished turn. It preserves the tool calls and results
and applies the change only to the outgoing request.
[Filter source](examples/ollama_tool_history.py)

1. As an Open WebUI admin, open **Admin Panel → Functions** and create a
   function. Enter the ID **ollama_tool_history** and the name **Ollama Tool
   History Compatibility**. Paste the contents of the Python file and save.
   Turn on the function and leave its **Global** switch off.
2. In **Workspace → Models**, edit the affected Ollama model and select
   the function under **Filters**. Leave **Start enabled in new chats** off for
   manual use, or turn it on for this model.
3. Reload the chat after saving the model settings. In the chat input's **+**
   menu, turn on **Ollama Tool History Compatibility** before sending or
   retrying the follow-up. Use the same switch to turn it off.

The filter uses the Open WebUI v0.13.4 request hook. You can copy the Python
file to a disconnected host and set it up there.
[Manage Open WebUI filters](https://docs.openwebui.com/features/extensibility/plugin/functions/filter/)

## GPU access

On a preparation host without NVIDIA GPU access, run `loaf status --cpu` and
start with `loaf run offline --cpu` or `loaf run online --cpu`. [CPU serving](launcher.md#serve-on-cpu)

In WSL2, use a Windows NVIDIA driver with WSL2 support. For both native Debian
and Debian in WSL2, configure the NVIDIA Container Toolkit on the Docker Engine
host. [Debian setup](debian.md)

The default `loaf-ollama-cuda13` build needs NVIDIA driver 580 or newer.
`nvidia-smi` on the host shows the driver version. For driver 550 to 579, set
`[images] ollama_image = loaf-ollama-cuda12` and run `loaf build` while
connected. [Ollama build selection](configuration.md#select-an-ollama-build-recipe)

```bash
(
    cd docker-project
    docker compose --profile offline exec ollama-offline nvidia-smi
    docker compose --profile offline exec ollama-offline ollama ps
)
```

`100% GPU` indicates full placement while the model is loaded. An empty list can
mean it is idle/unloaded. CPU offload can occur when weights, KV cache, buffers
and other GPU applications exceed VRAM. Reduce context or use a smaller model;
see [the hardware estimates](models.md).

## Imports and startup

```bash
(cd docker-project && docker compose --profile offline logs --tail 100 ollama-offline)
```

Check for the imported filename and `Ollama is ready for inference.`.
Each Modelfile's `FROM` paths must exist under the same `/ollama/models` mount.
Transfer weights/projectors and manifests along with exported text definitions.

A failed import prevents readiness and WebUI startup. Fix the source export or
missing blobs, then retry `loaf run offline`. Startup timeout values must
be positive integers. [Configuration](configuration.md#applying-changes)
explains the separate API-startup and Compose-health timeouts.

If saved Modelfiles changed while Ollama was running, run `loaf stop` and start
the desired serving mode again. Restarting interrupts active inference and
applies saved definitions. Use `loaf stop --no-remove-containers` to keep
containers and their logs for inspection before starting again.

If `loaf status` reports that no local model data was found, check the project
and namespace in the report against the ones you used to download or restore
the models. The probe checks both Ollama manifest layouts and also recognizes
saved Modelfiles with model blobs. For a fresh store, use `loaf download` on a
connected host or restore the models of a backup with `loaf restore models`.
[Separate namespaces](launcher.md#run-a-separate-namespace) · [Model transfer](transfer.md#transfer-models-between-existing-deployments)

## Downloads and selection files

Use `hf.co/owner/repository[:tag]`, without `https://`, quotes or inline comments.
A full-line `#` comment is allowed. If a short quantization tag is ambiguous,
select the exact merged GGUF filename as in the [Coder examples](models.md).
The launcher validates the entire list before stopping anything.

If `backup --export` reports a missing model, use `loaf download` with the same
`--models-file`, then retry the backup. Existing exports and registrations persist
after removing an entry from the selection. [Model selection](launcher.md#select-models)

## Browser connection

Open the address printed by `loaf run offline` or `loaf run online`. The default is
`http://localhost:3000`. In WebUI, check that the Ollama URL is
`http://ollama-offline:11434`. Offline WebUI uses the internal inference
network for Ollama and the `web` bridge for host port publishing. Online WebUI
uses the `connected` bridge.
A remote Debian UI can use the [SSH forward](debian.md#verify-and-open-webui).
Saved WebUI connection settings can override environment defaults.

## Terminal connection

Run `loaf status` with the terminal server enabled. Check its image under
**Local images and GPU** (or **Local images** with `--cpu`), its bearer key under **Configuration**, and its
runtime health in the container list. In Open WebUI, verify the direct connection to
`http://terminal-server:8000` and check the user's access grant. Open WebUI
starts with this connection only if it had none stored; a WebUI that first
ran without the terminal server needs it added in its admin settings.
[Enable and connect](terminal-server.md#enable-and-connect) If the
terminal works in the Files tab but the model answers with instructions,
check the model's Terminal capability, function calling mode and selected
terminal. [Terminal setup](terminal-server.md#enable-and-connect) ·
[Model settings](terminal-server.md#use-the-terminal-from-a-model)

If the Files tab shows dotfiles such as `.bashrc` but you are looking for a
generated file, ask the model for its absolute path and open its directory
with the Files breadcrumb. The **home** breadcrumb in a chat with the bundled
terminal lets you switch between `/home/user` and `/home/chat`.
To set the working directory for the model, use the **Working Directory**
folder button next to the terminal selector below the chat input.
[Terminal workspace folders](terminal-server.md#use-the-terminal-from-a-model)

## Packages in the terminal

Commands run as root, so `apt-get install` works in the chat's container.
When `apt-get update` cannot reach any source, the container has no route to
a mirror: with `[terminal_server] network = isolated`, set `apt_source_file`
and `apt_repository_dir` for a repository on the Docker host. With
`network = connected`, check that the Docker host reaches the mirror. Then run
`loaf run offline` or `loaf run online`. Packages installed in a chat stay in its
container only; a new chat starts without them.
[Install packages in a chat](terminal-server.md#install-packages-in-a-chat)

A new chat container has no package lists, so `Unable to locate package`
right after the chat starts means that `apt update` has not run yet.

When APT reports that a repository `is not signed`, mark a local
unsigned repository as trusted in the source file, for example
`deb [trusted=yes] file:/run/loaf-apt/repository ./`, or reference its key
with `signed-by=`. After an update, `Unable to locate package` means that the configured
source does not provide that name for the image's Debian release and
architecture.

## Build requests additional privileges

Buildx 0.37.2 and newer check each local file a Compose build reads.
Compose before 5.7.0 grants only the build contexts. Buildx also allows files
under its working directory, which is the project directory when `loaf` runs
Compose. The packaged images keep their Dockerfiles and default inputs inside
their contexts.

A terminal `apt_source_file` outside the project directory can still stop
`loaf build` with `additional privileges requested`, or with a prompt for
read access in a terminal. Move the file into the project, for example under
`config/`, or update Docker Compose to 5.7.0 or newer.
[Package mirror](terminal-server.md#package-mirror-for-image-builds) ·
[Compose build inputs](https://github.com/docker/compose/commit/43a5d5a253)

## Models or accounts missing after an upgrade

Releases up to 0.4.0 named the default deployment `local-inference`, with
the volumes `ollama` and `open-webui`. The new names start with `loaf`, so
the upgraded deployment starts with empty volumes while the old data stays
in place. `loaf docker ps` shows the new resources and `docker volume ls`
the old ones. Move the data with backup and restore.
[Upgrade from the local-inference names](launcher.md#upgrade-from-the-local-inference-names)

## Volume already exists but was not created by Docker Compose

Compose prints `volume "NAME" already exists but was not created by Docker
Compose` when a volume lacks Compose's project label. It still uses the
volume and its data. Backup and restore create missing `loaf-ollama` and
`loaf-open-webui` volumes with Compose's labels, so the warning appears
only for volumes created by other means, such as an older loaf release or a
manual `docker run`. Docker cannot add labels to an existing volume. To
silence the warning for good, back up the volume, remove it and restore it.

```bash
loaf backup webui
loaf docker rm containers
docker volume rm loaf-open-webui
loaf restore webui
```

## Launcher busy or interrupted

Wait for the other launcher in this checkout to finish. The `.run.lock` file can
remain normally after exit; the operating system releases the lock itself.
Avoid manual commands that start multiple Ollama services on the shared volume.
After interruption, completed downloads/exports remain and you can resume the
same mode. See [failure behavior](launcher.md#failure-and-concurrency-behavior).

If Debian reports a Docker socket permission error after a group membership
change, check the session's [Docker access](debian.md#docker-access).

## Command or project not found

Run `loaf` from the checkout or from the directory containing your
`docker-project` link. Use `--project-dir PATH` to select either location.
Use `./loaf` or activate `.venv` with `source .venv/bin/activate`.
If the project moved, run the [installer](debian.md#install-the-loaf-command)
again.
