# Make a release

[Project overview](../README.md)

GitHub and Gitea create a draft release when you push a version tag.
Each draft gets the `loaf` executable for Linux amd64, a separate
`loaf-v<version>-source.tar.gz` of the tagged repository, an optional Python
wheel, the standalone `LICENSE.txt`, and `SHA256SUMS`.
[GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github)

## Quick release

Run from the checkout root on `main` to release `v0.1.0` to the `github` remote.
For email privacy, update the current commit's author and committer email before
the first push. [GitHub commit email guidance](https://docs.github.com/en/account-and-profile/how-tos/email-preferences/setting-your-commit-email-address)

1. Review and stage tracked changes. Untracked installer caches stay out.

   ```bash
   git status --short
   git add -u
   git diff --cached --check
   git diff --cached --stat
   ```

2. Commit, push `main`, and preview the tag.

   ```bash
   git commit -m "Prepare Local AI Ferry v0.1.0" &&
   git push -u github main &&
   python3 scripts/release.py --dry-run github
   ```

3. Push the tag once **Verify** passes. The **Release** workflow builds and
   attaches the assets to a draft.

   ```bash
   python3 scripts/release.py -m "Initial public release of Local AI Ferry." github
   ```

4. After **Release** succeeds, review the draft and its attachments in GitHub
   **Releases** and click **Publish release**. You can also review and publish
   it with the GitHub CLI. [GitHub CLI reference](https://cli.github.com/manual/gh_release_edit)

   ```bash
   gh release view v0.1.0 --repo lfech/local-ai-ferry
   gh release edit v0.1.0 --repo lfech/local-ai-ferry --draft=false
   ```

## Run the executable

Download `loaf`, `LICENSE.txt`, and `SHA256SUMS` from Releases. From the
download directory, verify the downloads and set up a project.

```bash
sha256sum --check --ignore-missing SHA256SUMS
chmod +x loaf
mkdir loaf-project
cd loaf-project
../loaf setup
```

The executable supplies CPython, the launcher, deployment assets, and
the Python build requirements. It unpacks its runtime on first use under
`${XDG_DATA_HOME:-$HOME/.local/share}/loaf/runtime/`. The executable
recreates the runtime after `loaf clean` and uses `flock` to coordinate
cleanup with other invocations. Generated systemd units refer to the
original executable. [Python Build Standalone release](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110)

Setup creates `config/config.ini` and `config/models.ini` if missing,
creates the terminal server key, and links the packaged Docker project.
Use the same setup command with a binary carried in a backup. Follow it
with `loaf restore` when recovering a minimal deployment, or use
`loaf setup --all` to recover an archived source checkout and its Docker data.
For a fresh deployment, use `../loaf build` from the project directory.
[Backup recovery](transfer.md#debian-restore)

On Debian 13, run `../loaf setup --with-docker-deps` to have setup prepare
and install Docker. The host supplies the NVIDIA driver for GPU inference.
Use a connected machine for uncached Debian packages, Docker images, and
models. [Debian setup](debian.md) · [Disconnected installation](installation.md#disconnected-debian-installation)

## Standalone license

The build generates one `LICENSE.txt` for the executable. It includes the
project's BSD license, the upstream notices, and matching source links for
Berkeley DB, CPython, and the patches and build scripts used by Python Build
Standalone. Keep the file with the executable when redistributing it. The
executable also embeds the same file for offline reading with `./loaf --license`.
The matching source must remain available while the binary is distributed.
[Berkeley DB license](https://github.com/astral-sh/python-build-standalone/blob/20261110/LICENSE.bdb.txt)

## Push a version tag

1. Set the release version in [`python-package/pyproject.toml`](../python-package/pyproject.toml).
   Commit it, run `loaf test`, and push the release commit through your usual
   branch workflow.
2. From the checkout, run the release helper with Python 3.11 or newer and
   the Git remote you want to release to. It reads the package version from
   `HEAD`, creates the matching annotated tag, and pushes that tag.

   ```bash
   python3 scripts/release.py -m "Summarize the changes in this release" origin
   ```

   To release on both hosts, list both configured remotes, for example
   `origin gitea`. Use `-F release-notes.md` for longer notes, or omit both
   notes options to write them in your Git editor.
3. Open **Actions → Release** and check the run. Open **Releases**, review the
   draft and its five attachments, and publish it.

Add `--dry-run` to preview the tag, commit, and destinations. Omit the remotes
to create the tag locally. If a push fails, run the helper again with the
remaining remotes from the same commit. It reuses the existing annotated tag
and its original notes. You can also use the standard Git commands directly,
such as `git tag -a v0.1.0` and `git push origin refs/tags/v0.1.0`.

Both release jobs run the small disconnected handoff with the executable
they just built before attaching it to a draft.
[Release handoff check](verification.md#check-a-release-candidate)

Rerunning a release job refreshes its build attachments in an existing
draft. Use a new version and tag for changes after publication.
[GitHub workflow](../.github/workflows/release.yml) ·
[Gitea workflow](../.gitea/workflows/release.yaml)

## Gitea runner

Enable repository Actions and register a Linux amd64 runner with the
`ubuntu-latest` label. Its job environment needs Git, Bash, GNU tar,
Node.js for checkout, and Python 3.11 or newer. The runner needs access
to GitHub, Python package sources, the Gitea API, and the container and model
registries used by the handoff check. Its local Docker daemon must support
starting the disconnected Debian fixture with its own daemon.
Enable Releases and release attachments in Gitea. Allow the built-in job
token to write releases. If you use a reverse proxy, allow upload bodies
large enough for the executable. [Gitea Actions setup](https://docs.gitea.com/usage/actions/quickstart/) ·
[Job token permissions](https://docs.gitea.com/usage/actions/token-permissions/) ·
[Release attachment settings](https://docs.gitea.com/administration/config-cheat-sheet/#repository---release-repositoryrelease)

## Build and check locally

On a connected Linux amd64 machine, run `loaf binary` from the source
directory to build or update the ignored `dist/loaf` and `dist/LICENSE.txt`
files from your working files. From a fresh checkout, run
`PYTHONPATH=python-package/src python3 -m loaf binary`.
Source checkout backups run this build automatically.

The build uses compatible build wheels, including setuptools, from
`install-cache/wheels/` when available. It fetches the build requirements
when the cache cannot satisfy them. The terminal shows each build step
and prints pip's output if a step fails.

For a full set of release assets, use Python 3.11 or newer, check out the
commit you intend to tag, and supply its matching version tag. This build
uses the committed files. Use a separate output directory to keep the
release checksums with those assets.

```bash
bash scripts/build-release.sh v0.1.0 dist/release
(cd dist/release && sha256sum -c SHA256SUMS)
```

To use the optional wheel with an existing Python installation, verify the
downloaded wheel against `SHA256SUMS`, then run:

```bash
sha256sum --check --ignore-missing SHA256SUMS
python3 -m venv .venv
.venv/bin/python -m pip install --no-index ./local_ai_ferry-0.1.0-py3-none-any.whl
.venv/bin/loaf link-docker-project
```

For source-based development, extract the separate archive:

```bash
tar -xzf loaf-v0.1.0-source.tar.gz
cd loaf-v0.1.0
```

[Source setup](launcher.md) · [Installation guide](installation.md)
