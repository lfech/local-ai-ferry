# Upgrade

[Project overview](../README.md)

A new release brings its own deployment files. Settings stay in the project or
in `/etc/loaf`, and models, accounts and terminal folders stay in the Docker
volumes. Each upgrade ends with new images: build them while connected, or
load them from a backup made with the new release.

Compose no longer reads a `.env` file in the project unless `[launcher]
docker_env_enable = true`. Move its variables into `config.ini`, or enable the
setting. `loaf setup --force` now needs the parts to replace, such as
`loaf setup docs --force`.

## Upgrade a source checkout

After updating an older checkout, run the source setup command to install
`loaf`. A `docker-project` link from an earlier release is no longer used;
`loaf clean --full` removes it. Existing Compose project names, image tags and volume names carry
over to the new launcher.

## Upgrade the standalone executable

Replace the `loaf` file, then run `./loaf setup docs --force` in the
deployment to replace the exported guides with those of the new release.
Setup keeps your settings, and the new `loaf` brings its own deployment files.
Build the images while connected with `./loaf build`, or load newer images
from a backup. `./loaf version` shows the release in use.

## Upgrade the Debian package

Install the new release's package with apt, as for the
[first installation](installation.md#debian-package). dpkg asks before it
replaces a changed `/etc/loaf/config.ini`, and the package keeps an existing
terminal server key. Build the images while connected with `loaf build`, or
load newer images from a backup.

## Upgrade an existing terminal deployment

Rebuild with `loaf build`, then run `loaf run online` or `loaf run offline`.
Use `[terminal_server]` in the INI and `http://terminal-server:8000` for the
saved connection in Open WebUI.

Releases before v1.0.0 do not migrate terminal data. This release ignores the
per-account volumes, per-chat volumes and chat package images of earlier
releases. Copy the files you need from them, then remove them. Move the user
volume of release 0.4.0 with backup and restore as described in
[Upgrade from the local-inference names](#upgrade-from-the-local-inference-names).
List the older resources with:

```bash
docker volume ls --filter label=loaf.tool-server.workspace
docker volume ls --filter label=loaf.terminal.chat-project
docker image ls local/loaf-terminal-chat
```

## Upgrade from the local-inference names

Releases up to 0.4.0 named the default deployment `local-inference`. Its
images were `local/NAME:latest`, its volumes `ollama` and `open-webui`, and
the terminal user volume `loaf-tools-HASH-users`. Every name now starts with
the deployment namespace `loaf`, or `loaf-NAME` for a separate namespace.
Docker cannot rename volumes or images, so the new release starts with empty
volumes until you move the data.

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

[Docker names](cli-help.md#docker)
