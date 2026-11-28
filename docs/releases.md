# Make a release

[Project overview](../README.md)

GitHub and Gitea create a draft release when you push a version tag.
Each draft gets the `loaf` executable for Linux amd64, a separate
`loaf-v<version>-source.tar.gz` of the tagged repository, an optional Python
wheel, the `loaf_<version>_all.deb` Debian package, the standalone
`LICENSE.txt`, and `SHA256SUMS`. The wheel and the Debian package carry the
same standalone executable, which backups from them include.

[GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github)

## Quick release

Run from the checkout root on `main` to release `v0.1.0` to the `github` remote.
For email privacy, update the current commit's author and committer email before
the first push.

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
   it with the GitHub CLI.

   ```bash
   gh release view v0.1.0 --repo lfech/local-ai-ferry
   gh release edit v0.1.0 --repo lfech/local-ai-ferry --draft=false
   ```

[GitHub commit email guidance](https://docs.github.com/en/account-and-profile/how-tos/email-preferences/setting-your-commit-email-address) ·
[GitHub CLI reference](https://cli.github.com/manual/gh_release_edit)

## Executable size

The executable holds a compressed runtime: CPython, pip, the setuptools
wheel, the launcher and its deployment data. The build leaves out what loaf
never runs, which shrinks it from about 40 MB to about 22.5 MB:

| Left out | Uncompressed | Why loaf does not need it |
| --- | --- | --- |
| `lib/libpython3.13.so` | 31 MB | `bin/python3.13` links libpython statically. |
| Tcl/Tk, `tkinter`, IDLE, turtle | 10 MB | They need a desktop session. |
| `share/terminfo`, manual pages, `include/`, `python3-config` | 10 MB | They serve interactive terminals and C extension builds. |
| `ensurepip` | 2 MB | The runtime creates no virtualenvs with pip. |

The build checks that the launcher, `unittest`, `sqlite3`, `ssl`, `venv`,
`urllib` and pip still import, and runs its usual setup, guide and dry-run
checks with the result. The payload uses gzip at level 9.

These steps would shrink it further but break something, so the build does
not take them:

| Step | Size | What it breaks |
| --- | --- | --- |
| xz instead of gzip | about 15.5 MB | The launcher unpacks with `tar`, and minimal Debian systems such as the `debian:12-slim` and `debian:13-slim` images lack `xz`. |
| Leave out pip and setuptools | about 18.2 MB | The executable builds the launcher wheel offline for `installers` in a source checkout whose `.venv` is missing. |
| Leave out further standard library modules | below 1 MB each | pip, `loaf test` and the transfer code use most of them. |

The remaining bulk is `bin/python3.13` itself, about 12 MB compressed. A
smaller interpreter needs a custom CPython build instead of the pinned Python
Build Standalone release.

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

Rerunning a release job refreshes its build attachments in an existing
draft. Use a new version and tag for changes after publication.

[Release handoff check](verification.md#check-a-release-candidate) ·
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
large enough for the executable.

[Gitea Actions setup](https://docs.gitea.com/usage/actions/quickstart/) ·
[Job token permissions](https://docs.gitea.com/usage/actions/token-permissions/) ·
[Release attachment settings](https://docs.gitea.com/administration/config-cheat-sheet/#repository---release-repositoryrelease)

## Build and check locally

`loaf package` builds release files from your working files into the ignored
`dist/` directory. Name one or both targets. From a fresh checkout, run
`PYTHONPATH=python-package/src python3 -m loaf package binary debian`.

```bash
loaf package binary debian
```

`binary` builds or updates `dist/loaf` and `dist/LICENSE.txt` on a Linux
amd64 machine. It fetches the pinned standalone Python runtime and its license
sources, checks their SHA-256 hashes and keeps them in `install-cache/python/`,
so later builds work offline. The `installers` backup type carries them.
Source checkout backups run this build automatically.

`debian` builds `dist/loaf_<version>_all.deb` with
`scripts/build-release.sh --debian-only`: the standalone executable, a wheel
that carries it, and the package from that wheel with
[`scripts/build-deb.sh`](../scripts/build-deb.sh). It needs `dpkg-deb` and a
Linux amd64 machine, so run it on Debian or Ubuntu.

Both targets use compatible build wheels, including setuptools, from
`install-cache/wheels/` when available. They fetch the build requirements
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

The release build also creates the Debian package from the release wheel,
which carries the standalone executable, as `loaf package debian` does. It
runs `lintian` when available; the package's lintian overrides explain the
expected tags. Set `LOAF_DEB_MAINTAINER` to the name and address for the
package's `Maintainer` field. The release workflows install the package in
the disconnected handoff destination.

```bash
bash scripts/build-deb.sh dist/release/local_ai_ferry-0.1.0-py3-none-any.whl dist/release
```

To use the optional wheel with an existing Python installation, verify the
downloaded wheel against `SHA256SUMS`, then run:

```bash
sha256sum --check --ignore-missing SHA256SUMS
python3 -m venv .venv
.venv/bin/python -m pip install --no-index ./local_ai_ferry-0.1.0-py3-none-any.whl
.venv/bin/loaf setup
```

For source-based development, extract the separate archive:

```bash
tar -xzf loaf-v0.1.0-source.tar.gz
cd loaf-v0.1.0
```

[Debian package](installation.md#debian-package) · [Source setup](launcher.md) ·
[Installation guide](installation.md)
