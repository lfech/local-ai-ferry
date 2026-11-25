# Verification record

[Project overview](../README.md) · [Verification guide](verification.md)

Older entries use the current command and project names in examples.

## 2026-11-25 16:55:23 +0100 — Chat-scoped APT packages

- The regular Docker suite passed 231 of 250 cases. Its 19 skips covered
  CPU/GPU model inference, catalog, Open Terminal opt-ins and API
  dependencies, and the host rsync fixture. The Docker-free run passed
  199 of 250 cases with 51 skips.
- Two focused tests passed on Debian WSL2 with Docker Engine 26.1.5 and
  Compose 2.26.1-4. They used the loaded terminal image and a local
  repository with the installer network disabled. The package test
  verified a real dependency and post-install script, use from both
  working directories, a failed install, chat ownership, saved and
  temporary chat isolation, restart persistence, and deletion cleanup.
  The other test verified the read-only mount and source edits.
- All five provisioner tests passed inside the loaded image with
  networking disabled. All 24 focused configuration, readiness, terminal
  settings, and documentation checks passed. The five standalone
  documentation checks also passed.

## 2026-11-25 15:52:12 +0100 — Runtime terminal APT source

- On Debian 13.8 in WSL2 with Python 3.13.5, the installed CLI suite
  passed 199 of 249 cases with Docker disabled. The 50 skips covered
  Docker, CPU/GPU model inference, catalog, Open Terminal opt-ins and
  API dependencies, and the host rsync fixture.
- The focused Docker test passed on Engine 26.1.5 and Compose 2.26.1-4
  with a previously loaded image and `--no-build --pull never`. It
  verified read-only access in an unprivileged workspace, live in-place
  edits, and pickup after replacing the file and restarting the service.
- The focused configuration, terminal settings, readiness, and
  documentation suite passed all 24 cases. All five standalone
  documentation checks passed. An offline wheel build included the
  Compose overlay and both Open Terminal scripts.

## 2026-11-25 15:37:10 +0100 — Selectable transfers and terminal key

- On Debian 13.8 in WSL2 with Python 3.13.5, 229 of 246 cases passed with
  the regular Docker suite enabled. The 17 skips covered CPU/GPU model
  inference, catalog, Open Terminal opt-ins and API dependencies, and
  the host rsync fixture. The Docker model-only case verified both merge
  and mirror through the installed CLI.
- A focused real Docker round trip verified the terminal workspace and the
  exact archived key with mode `0600` from both full and volumes-only backups.
- The final transfer rerun passed 46 of 47 cases with the host rsync fixture
  skipped. It exercised all seven component combinations and verified that
  a project-only restore leaves the archived terminal key for the later
  volumes restore. All five documentation checks passed.

## 2026-11-25 15:19:44 +0100 — Compatibility audit and CPU verification

- Compared the committed and pre-cleanup working trees on Debian 13.8 in
  WSL2 with Python 3.13.5. The Docker-free suites passed 183 and 188 cases
  respectively, each with 48 skips.
- The final core run passed 230 of 239 cases on Docker Engine 26.1.5 and
  Compose 2.26.1-4, with the catalog, rsync, and local Open Terminal API
  checks enabled. The nine skips were covered separately by three passing
  Open Terminal Docker cases and six passing CPU model cases.
- The CPU cases passed with Ollama 0.40.18, including real inference at the
  configured 16,384-token context, 100% CPU placement, and live archive
  mirroring. GPU-specific placement and long-prompt assertions were outside
  this run. The rebuilt Open Terminal image used a disposable tag.
- The initial integration runs surfaced stale help assertions and a
  Compose fixture that assumed an alias mapping. The corrected fixtures
  passed in the final core run. The combined CPU run ended after CPU
  fixture cleanup to avoid repeating the already-verified groups.
- A cold restore with the built Linux amd64 standalone launcher recovered
  a copied source checkout and both Docker fixture volumes. The relocated
  launcher verified the retained archive. The standalone build used the
  upstream setuptools wheel after Debian's wheel failed in bundled Python.
  The wheel smoke check and all five documentation checks also passed.
  [Verification commands](verification.md)

## 2026-11-25 14:44:36 +0100 — Terminal key setup and in-place backup recovery

- On Debian 13.8 in WSL2 with Python 3.13.5, the Docker-free suite passed
  188 of 236 cases. Transfer fixtures used simulated Docker and covered
  terminal key reuse, minimal and full recovery, archive link rejection,
  and rollback when the launcher move failed.
- The 48 skips covered 41 opt-in Docker, GPU, Open Terminal Docker and catalog
  cases, six Open Terminal API cases with missing dependencies, and one
  local model-transfer case requiring rsync.
- All five standalone documentation checks passed using the offline editable
  installation. [Verification commands](verification.md)

## 2026-11-24 00:49:12 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.2.
Unittest reported 231 cases and 37 skips.
Enabled test options were `RUN_TERMINAL_SERVER_TESTS=1`.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 184/184 |
| Open Terminal | 10/10 |

## 2026-11-24 00:43 CET — Open Terminal workspace labels

- Reproduced both workspace failures on Debian 13.8 in WSL2. The fixtures
  filtered by `loaf.*` and could not find the chat volumes labeled `owo.*`.
- All four Open Terminal Docker cases passed on Engine 26.1.5 and Compose
  2.26.1-4 with a disposable image containing the updated provisioner. The
  connected and offline cases checked both accounts' volumes before and after
  cold restart, and the test projects' workspace resources were removed.
- The baseline passed 190 cases with 41 skips on Python 3.13.5. The six local
  Open Terminal API checks ran. The skips were Docker, GPU, Open Terminal
  Docker, and catalog cases that require opt-in. [Test commands](verification.md#terminal-server-checks)

## 2026-11-24 00:30:09 +0100 — Test badge run

`loaf test --badges` failed on Linux with Python 3.13.2.
Unittest reported 231 cases and 6 skips.
Enabled test options were `RUN_DOCKER_TESTS=1`, `RUN_GPU_TESTS=1`, `RUN_CATALOG_TESTS=1`, `RUN_TERMINAL_SERVER_TESTS=1`.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 184/184 |
| Docker | 30/30 |
| Open Terminal | 2/4 |
| GPU inference | 6/6 |
| Model catalog | 1/1 |

## 2026-11-24 00:06 CET — Local network access guide

- All five documentation checks passed. Both WebUI INI examples loaded using
  the launcher configuration parser.
- Windows PowerShell parsed all ten examples and confirmed the firewall
  parameters against the installed cmdlets. In-memory fixtures checked the
  NAT IPv4 lookup and the mirrored-to-NAT configuration edit. Verification
  covered configuration, command syntax and string handling.

## 2026-11-23 22:51 CET — Cleanup and GitHub Verify checks

- Reproduced the CI cleanup failure locally. The fixture expected Git-visible
  files in a retired installer directory to be removed.
- Nine cleanup tests and five documentation tests passed on Debian 13 WSL2
  with Python 3.13.5. The Verify workflow steps passed in a disposable checkout
  using the staged copies of the local INI files that required owner access.
  The Docker-free suite passed 184 cases with 47 skips, and the fresh wheel
  passed the license, Compose data and command smoke checks.

## 2026-11-23 22:33 CET — GitHub quick release guide

- All five documentation checks passed. The tests checked the Markdown links
  and Bash syntax for the new [Quick release](releases.md#quick-release) section.

## 2026-11-23 22:20 CET — Publication audit and ignore rules

- Git verified 48 ignored paths and 11 visible paths. All 98 tracked files
  were visible under the updated rules. A local detect-secrets scan of the
  current checkout flagged only fixture values, key path settings and pinned
  checksums. The local terminal bearer key was ignored and untracked.
- On Debian 13 WSL2 with Python 3.13.5, a disposable checkout built the
  Linux amd64 executable, wheel and source archive. All four artifact
  checksums and the embedded license matched. The tagged source archive
  contained no Git history, local keys, installers, images or model files.
  The locally built executable carried the build account in its tar headers.
- The Docker-free suite passed 188 cases and skipped 42. Docker, GPU,
  Open Terminal Docker and model catalog opt-ins were disabled. All four
  GitHub and Gitea workflow files parsed locally.

## 2026-11-23 22:01 CET — Ollama tool history filter

- On Debian 13 WSL2, an isolated CPU Ollama 0.40.17 reproduced the Jinja
  role-order error when a user followed a tool result without an assistant
  reply. The same history rendered with Qwen3-14B, and the cached Ministral
  rendered it after an empty assistant turn was added. The rendered prompt
  still included the tool call, its result and the follow-up.
- In an isolated Open WebUI v0.13.4 instance, the native Ollama chat returned
  HTTP 500 for the interrupted history with the filter off and HTTP 200 with it
  on. Non-streaming worked through a workspace model, and streaming worked
  on the base model after refreshing the model list. A model without the
  filter assigned still rejected the history. A normal request also passed.
  The probes used a temporary database and mounted the cached models read-only.
- The Docker-free suite passed 188 cases and skipped 42 with Python 3.13.5.
  The restricted sandbox stalled in an Open Terminal fixture. The completed
  run allowed local sockets and used a temporary editable installation.
  Docker, GPU, Open Terminal Docker and catalog opt-ins were off. All five
  filter and five documentation cases passed separately.

## 2026-11-23 17:43 CET — Standalone license and release checks

- On Debian 13 WSL2, a local standalone build and a tagged build in a
  disposable Git checkout produced matching `LICENSE.txt` files. Each
  launcher printed its exact accompanying file. All four entries in the
  tagged build's `SHA256SUMS` verified.
- The published Berkeley DB 6.0.19 and CPython 3.13.18 source archives
  downloaded successfully and matched the pinned build manifest's SHA-256
  digests. Mocked GitHub and Gitea draft creation and refresh each attached
  all five assets.
- The Docker-free suite passed 177 cases and skipped 48 with Python 3.13.5.
  Docker, GPU, Open Terminal Docker and model catalog opt-ins were disabled.
  All five documentation tests passed separately.

## 2026-11-23 17:33:11 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 225 cases and 48 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 177/177 |

## 2026-11-23 17:30:32 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 225 cases and 48 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 177/177 |

## 2026-11-23 17:29 CET — Repeated badge refresh checks

- Before the change, `loaf test --badges` updated the README and verification
  record while the CLI SVG stayed byte-identical on the same day.
- After the change, two consecutive Docker-free runs on Debian 13 WSL2 with
  Python 3.13.5 each passed 177 cases with 48 skips. Their CLI badges showed
  different times. All four skipped groups kept their previous badge files.
- Four focused package checks and five documentation checks passed. The
  rebuilt standalone launcher generated a timestamped badge in a disposable
  project.

## 2026-11-23 17:28:43 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 225 cases and 48 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 177/177 |

## 2026-11-23 17:28:32 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 225 cases and 48 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 177/177 |

## 2026-11-23 17:26:30 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 224 cases and 48 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 176/176 |

## 2026-11-23 17:24 CET — Publication review checks

- The Docker-free suite passed 176 cases with 48 skips on Debian 13 WSL2
  with Python 3.13.5. Docker, GPU, Open Terminal Docker and model catalog
  opt-ins were disabled. All five documentation checks passed separately.
- All ten Compose configuration checks passed on Compose 2.26.1. The checks
  rendered service configuration without starting containers.
- A tracked-file scan found no private-key headers, common token patterns or
  URLs with embedded credentials. Git ignores the deployment bearer key,
  working backups and build artifacts.

## 2026-11-23 17:20:21 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 224 cases and 48 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 176/176 |

## 2026-11-23 17:17:38 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 224 cases and 48 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 176/176 |

## 2026-11-23 17:16 CET — Terminal image backup checks

- The Docker-free suite passed 176 cases with 48 skips on Debian 13 WSL2
  with Python 3.13.5. Docker, GPU, Open Terminal Docker and model catalog
  opt-ins were disabled. The backup fixtures simulated Docker and checked
  image selection, omission, verification and restore with and without
  workspace data.
- The focused transfer and help run passed 39 cases with one skip.
- All five documentation checks passed. The rebuilt standalone launcher
  completed setup and previewed both terminal image selections.

## 2026-11-23 17:11 CET — Local AI Ferry verification

- On Debian 13 WSL2 with Python 3.13.5, the Docker-free suite passed 173
  cases with 48 skips, and all five documentation checks passed. Docker,
  GPU, Open Terminal Docker and model catalog opt-ins were disabled.
- All ten Compose configuration checks passed with Compose 2.26.1. An
  additional render verified legacy and current namespace and terminal
  settings. Neither check started containers.
- A fresh wheel installation linked the Docker project and previewed CPU
  serving. The standalone build produced `dist/loaf`, which also completed
  setup and previewed CPU serving from a fresh directory. The first build
  lacked network access, and the approved runtime fetch completed on retry.
- A pre-rename backup passed its bundled executable checksum and was found
  by the editable and standalone restore previews. The suite verified and
  restored a legacy bundle with simulated Docker calls.

## 2026-11-23 15:47:15 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 218 cases and 48 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 170/170 |

## 2026-11-23 15:45 CET — Command and flag help checks

- All eight focused help and integration checks and all five documentation
  checks passed on Debian 13 WSL2 with Python 3.13.5. Help checks covered flag
  scope, aliases, argument placement and help before project validation.
  The installed `loaf` and `python -m loaf` entry points printed help, and all
  21 command and integration entries were checked against the reference.
- The Docker-free suite passed 170 cases with 48 skips. Docker, GPU,
  Open Terminal Docker and catalog opt-ins were disabled. Optional `aiohttp`,
  `open_terminal` and `rsync` checks lacked their dependencies.

## 2026-11-23 15:32 CET — Release tagging checks

- All seven release helper checks and five documentation checks passed on
  Debian 13 WSL2 with Python 3.13.5 and Git 2.47.3. Temporary Git repositories
  exercised Markdown notes, tag-only pushes, retry after a partial push,
  version checks and existing tag protection. The checkout preview reported
  the expected tag, commit and remote. Pushes used local test repositories.
- The Docker-free suite passed 166 cases with 48 skips. Docker, GPU,
  Open Terminal Docker and catalog opt-ins were disabled. Optional `aiohttp`,
  `open_terminal` and `rsync` checks lacked their dependencies. The initial
  invocation set a legacy bridge override that interfered with one config
  test. The rerun cleared that override and passed.

## 2026-11-23 15:24:02 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 207 cases and 48 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 159/159 |

## 2026-11-23 15:21 CET — Cleanup and safety checks

- The final Docker-free suite passed 160 tests, with 47 skips, on Debian
  13 WSL2 and Python 3.13.5. The five documentation checks, Ruff
  correctness checks, Python compilation and Bash syntax checks passed.
- The extended suite passed 192 tests, with 12 skips, using Docker Engine
  26.1.5 and Compose 2.26.1. It included the Docker, Open Terminal and
  public model catalog groups. GPU inference was disabled. The local
  proxy and runner cases that lacked Python dependencies in that run
  passed in separate checks using a temporary aiohttp installation and
  the existing Open Terminal image.
- A rebuilt standalone executable verified both cleanup modes, dry-run,
  runtime lock contention, and re-creation of the cache and Docker link
  on startup. A disposable source virtualenv completed a full clean while
  running the command. The tagged release artifacts passed their checksum
  checks.

## 2026-11-23 14:13 CET — Build wheel reuse and output

- Rebuilt `dist/loaf` with pip indexes disabled and confirmed the bundled
  setuptools wheel matched the cached file byte for byte.
- A tagged release build fetched the requirements when the local cache
  contained an incompatible wheel. Both successful runs showed only
  workflow messages, and the tagged assets passed their checksum checks.
- Invalid package metadata caused a wheel build failure. The build
  printed the pip error, returned nonzero and preserved the previous
  executable.
- The Docker-free suite passed 150 tests with 47 opt-in skips on Debian 13
  WSL2 and Python 3.13.5. All five documentation checks passed.

## 2026-11-23 14:04 CET — Local binary and backup build

- The Docker-free suite passed 150 cases, with 47 skips, on Debian 13 WSL2
  with Python 3.13.5. Docker, GPU, Open Terminal and catalog opt-ins were
  disabled. The five standalone documentation checks passed.
- `loaf binary` created and then replaced the executable at `dist/loaf`.
  The shared build ran setup and previewed CPU serving from the executable.
- In a temporary checkout, a source backup built and bundled an executable
  with an uncommitted default-setting change. Setup picked up that change
  and restore recovered the saved setting. Docker commands were simulated.
  A failed build in the test suite stopped backup before Docker calls.
- The tagged-asset build used committed source files and produced an
  executable, wheel and source archive that passed their checksum checks.

## 2026-11-23 13:15 CET — Backup folder and standalone setup

- The Docker-free suite passed 148 cases, with 47 skips. Docker, GPU,
  Open Terminal and catalog opt-ins were disabled on Debian 13 WSL2.
  The five standalone documentation checks passed.
- A rebuilt Linux amd64 executable ran setup from a fresh directory with
  Python and Git absent from `PATH`. It created the two default config
  files and Docker link without a checkout or virtualenv.
- The executable created a versioned backup, and the copied executable
  verified and restored it from both the backup folder and a separate
  writable project. The restored settings, Modelfile, and image and volume
  streams matched the fixtures. Docker commands were simulated.
- Focused tests rejected missing bundle files and a changed executable
  before Docker. They also exercised local-only installer recovery and
  verified that a failed volume restore preserved the destination config.

## 2026-11-23 12:55 CET — Standalone release verification

- The Docker-free suite passed 146 cases with 47 skips on Debian 13 WSL2 and
  Python 3.13.5. Docker, GPU, Open Terminal and catalog opt-ins were disabled.
  The five standalone documentation checks passed.
- The release build produced an approximately 39 MiB executable with
  CPython 3.13.18 and verified all three asset checksums. The separate
  source archive matched every tracked file in the bundled project.
- With Python and Git absent from `PATH`, the executable created a project
  and installed an editable launcher in `.venv` without Python package
  indexes. It then repaired a copied project's virtualenv using the
  bundled interpreter and cached wheels. Host package commands were
  simulated. Compose build and CPU serving were previewed.
- Both workflows passed YAML and Bash syntax checks. A mock GitHub CLI and
  a loopback Gitea API fixture exercised draft creation and four-asset
  replacement. Hosted release runs were outside this check.

## 2026-11-23 12:44 CET — Installer cache and release preparation

- The Docker-free suite on Debian 13 WSL2 with Python 3.13.5 passed 144
  cases with 47 skips. Docker, GPU, Open Terminal and catalog opt-ins were
  disabled. The five standalone documentation checks passed.
- APT downloaded 48 Debian 13 amd64 packages using an empty package-status
  file. A second download-only run resolved and read all 48 from the local
  cache in a checkout path containing spaces. APT made no host package
  changes.
- A copied checkout reused the relocated wheel cache to create a new
  virtualenv and install the launcher with package indexes unavailable.
  The installed launcher linked Compose and previewed CPU serving.
- The release build produced and verified the version `0.1.0` wheel and
  source distribution, and rejected a mismatched tag. Both workflows
  passed YAML and Bash syntax checks. A mock GitHub CLI and a local Gitea
  API fixture exercised draft creation, attachment replacement and an
  already published release. Hosted release runs were outside this check.

## 2026-11-23 11:20 CET — GPU readiness error

- On the Debian WSL2 host without NVIDIA GPU access, `loaf status` returned
  exit 1 with a one-line GPU failure and the `loaf status --cpu` command.
  CPU preflight passed with exit 0.
- Both readiness tests and all five documentation checks passed on Python
  3.13.5. The Docker-free suite passed 142 cases with 47 opt-in or
  dependency skips.

## 2026-11-23 10:48 CET — Selective badge verification

- All 37 focused package, configuration and documentation checks passed
  under Python 3.13.5. The badge fixtures exercised partial groups,
  all-skipped runs, and failed and skipped subtests.
- The Docker-free `loaf test --badges` run passed 142 cases with 47 skips,
  using rsync and all integration opt-ins disabled. It refreshed the CLI
  badge and verified that the four skipped badges and their README links
  had identical contents before and after the run.
- The initial checkout run exposed editable metadata lookup errors.
  Preserving the test discovery import path resolved them on rerun.

## 2026-11-23 10:48:18 +0100 — Test badge run

`loaf test --badges` passed on Linux with Python 3.13.5.
Unittest reported 189 cases and 47 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 142/142 |

## 2026-11-23 10:47:27 +0100 — Test badge run

`loaf test --badges` failed on Linux with Python 3.13.5.
Unittest reported 189 cases and 47 skips.
All opt-in test options were disabled.

| Group | Passed / executed |
| --- | ---: |
| CLI baseline | 140/142 |

## 2026-11-23 10:37 CET — Dated README badges

- All five documentation checks passed. The five SVG dates matched their
  linked run records, and each pass count appeared on the right.
- On Python 3.13.5 with all four `RUN_*_TESTS` flags set to `0` and rsync
  available, `loaf test -v` passed 139 of 186 discovered cases with 47
  advanced or optional dependency skips. The README and badge files had
  identical contents before and after the run.

## 2026-11-23 10:31 CET — README test badge runs

The development PC ran Debian 13 in WSL2. Host tests used Python 3.13.5.
The Docker checks used cached images, Docker Engine 26.1.5 and Compose
2.26.1-4.

| Group | Passed / total | Run details |
| --- | ---: | --- |
| CLI baseline | 139/139 | `loaf test` discovered 186 cases with all four `RUN_*_TESTS` flags set to `0`. It skipped 47 advanced cases, including six Open Terminal API cases whose optional packages were absent from the virtualenv. The local rsync case used Debian's rsync 3.5.0 unpacked under `/tmp`. |
| Docker | 30/30 | All four opt-in Docker test classes passed. |
| Open Terminal | 10/10 | Four disposable Docker integration cases passed in the same run as the Docker group. Five provisioner tests and one runner timeout test passed separately in the cached terminal image with networking disabled. |
| GPU inference | 6/6 | Results from the November 22 RTX 4070 Ti SUPER run. [GPU verification](#2026-11-22-1233-cet--independent-docker-checks-and-quieter-test-output) |
| Model catalog | 1/1 | The live Hugging Face check passed for all five distinct model references. |

All five documentation checks passed. The README's badge images and their
links were checked against the recorded counts.

## 2026-11-23 10:12 CET — CPU serving

- On Debian 13 in WSL2 with Docker Engine 26.1.5 and Compose 2.26.1-4,
  both `loaf online --cpu` and `loaf offline --cpu` passed in a disposable
  deployment. The NVIDIA runtime reported no WSL GPU adapters. Both
  Ollama containers had empty GPU device requests. Ministral generated
  tokens in each profile and reported zero VRAM usage. WebUI and Open
  Terminal were healthy, and `loaf status --cpu` passed in both profiles.
  The test copied the cached model store through a read-only mount.
- With Python 3.13.5, all five documentation checks passed. The Docker-free
  suite ran **186 cases with 138 passes and 48 skips**. With Docker checks
  enabled, **168 passed and 18 skipped**, including successful CPU and GPU
  Compose configuration checks. GPU inference, terminal acceptance and
  catalog tests were opted out. The local terminal API and rsync checks
  skipped for missing dependencies.
- Built and installed a wheel with package indexes disabled. The linked
  project resolved the packaged CPU override in the launcher preview.

## 2026-11-22 21:33 CET — Architecture guide

- Checked the guide against the launcher, Compose configuration, terminal
  provisioner and transfer code. All five documentation checks passed.
- The fast suite ran **183 cases with 137 passes and 46 opt-in or dependency
  skips**. Both runs used a temporary editable Python 3.13.5 environment
  because the checkout's interpreter was inaccessible.

## 2026-11-22 21:28 CET — Default entries in the working directory menu

- Verified the active Open WebUI v0.13.4 bundle reads per-path history
  records and displays up to eight recent entries. The two terminal
  integration cases passed in separate online and offline projects on
  Debian 13.8 in WSL2 with Engine 26.1.5 and Compose 2.26.1-4. They
  covered empty and populated histories, retention of existing records,
  account scope, and setting the working directory in a saved chat.
- The Python 3.13.5 fast suite ran **183 tests with 137 passes and 46
  opt-in or dependency skips**. The five documentation checks and five
  provisioner checks passed.
- Built the production terminal image with Compose. Verified that
  `local-inference` was healthy with the new image after its launcher
  finished. Live API requests returned both default entries and
  confirmed read-only draft and writable saved chat directories.

## 2026-11-22 21:11 CET — Chat folders in the working directory picker

- The Python 3.13.5 fast suite passed **137 of 183 cases** in a temporary
  editable environment, with 46 integration or dependency skips. All five
  documentation checks and five provisioner API and policy checks passed.
- Both online and offline terminal integration cases passed on Debian 13.8
  in WSL2, using Engine 26.1.5 and Compose 2.26.1-4. They verified both
  draft directories, the read-only `chat` placeholder, applying the
  selection to a saved chat and using both writable directories in model
  commands. The checked paths also exercised restart and chat deletion.
- Confirmed the running `local-inference` image matched the updated
  provisioner. The live account and saved chat runners both reported
  `/home` as the browser root and listed `user` and `chat`. Verification
  used the APIs that back the picker and checked the picker implementation
  in Open WebUI v0.13.4.
  [Open WebUI working directory picker](https://github.com/open-webui/open-webui/blob/v0.13.4/src/lib/components/chat/MessageInput/WorkingDirectoryMenu.svelte)

## 2026-11-22 21:03 CET — Live chat folder navigation

- Checked the running `local-inference` deployment with WebUI v0.13.4. The
  provisioner used the updated image and the read-only WebUI volume mount.
  The saved chat runner had both volumes. Its Files API reported the
  browser root as `/home` and listed `chat` and `user` there. The account
  runner reported `/home/user` as its browser root.
- Reviewed how WebUI retains the draft's file view when saving a new chat.
  Used the runner APIs for live verification. Browser interaction was
  outside this check. All five documentation checks passed.
  [Open WebUI Files component](https://github.com/open-webui/open-webui/blob/v0.13.4/src/lib/components/chat/FileNav.svelte#L849-L879)

## 2026-11-22 20:53 CET — Chat folders in Open Terminal

- On Debian 13.8 in WSL2 with Python 3.13.5, ran **183 fast tests with 137
  passes and 46 skips**. The checkout interpreter was inaccessible, so the
  tests and five documentation checks used a temporary editable install
  from cached dependencies. Built a wheel offline and matched its three
  changed deployment assets against the source checkout.
- All **34 Docker and command tests** and **four terminal integration
  tests** passed on Engine 26.1.5 and Compose 2.26.1-4. The terminal
  fixtures used a separately tagged image built from cached layers.
  The serving cases exercised both profiles, model command and Files API
  access to directories, account and chat isolation, archive and restart
  retention, and volume removal after single and bulk chat deletion.
  Earlier runs exposed the upstream starting-directory restriction and
  the WebUI's 403 response for inaccessible chats. The tests passed after
  retaining the home starting directory and updating the response assertions.
- All **six provisioner and runner API checks** passed in network-disabled
  containers. Additional Docker checks confirmed that the unprivileged
  runner can write to the named and temporary chat mounts. Across these
  runs, 176 distinct tests passed. The six GPU cases and the public model
  catalog check were skipped.

## 2026-11-22 12:52 CET — Separate third-party notices

- Built and installed a fresh wheel offline using Python 3.13.5. The installed
  license and notices matched the repository copies. All six focused checks
  passed with no skips, including five documentation checks. `actionlint`
  validated the updated GitHub workflow.

## 2026-11-22 12:50 CET — Third-party license notices

- The repository and package license files matched byte for byte. Built
  and installed a fresh wheel offline using Python 3.13.5. All six selected
  checks passed with no skips, including the installed-license comparison
  and all five documentation checks.

## 2026-11-22 12:40 CET — GitHub working-tree quality audit

- On Debian 13.8 in WSL2, installed the current source snapshot offline on
  Python 3.10.22 and 3.13.5. Each Docker-free run passed **137 of 182 cases**.
  The 45 skips covered opt-in integration checks and optional terminal API
  dependencies. Fresh wheels built and installed offline on both versions.
  The installed license, linked Compose and terminal assets, download
  preview and Tether CLI output passed their checks.
- All **34 focused Docker and command cases** passed with Engine 26.1.5,
  Compose 2.26.1-4 and cached images in disposable projects. The five
  documentation checks passed.
- `actionlint` 1.7.12 validated the GitHub workflow locally. Gitleaks
  8.30.1 reported zero findings in 72 publishable working-tree files.
  The scan excluded Git history and ignored machine-local data. Confirmed
  ignore rules for terminal keys, `.env`, deployment archives, virtualenvs,
  and wheel caches. Hosted GitHub execution and settings are unverified.

## 2026-11-22 12:33 CET — Independent Docker checks and quieter test output

- On Debian 13.8 in WSL2 with Python 3.13.5, the fast installed suite ran
  **181 cases with 136 passes and 45 opt-in or dependency skips**. With
  Docker Engine 26.1.5, Compose 2.26.1-4, and Docker, terminal and catalog
  checks enabled, the full suite had **170 passes and 11 skips**.
- All six GPU cases passed in 550 seconds on the RTX 4070 Ti SUPER. They
  checked the exported Modelfiles, manifest recovery, explicit replay,
  WebUI startup after removing an invalid Modelfile, a connected model
  archive round trip, and full GPU placement at 16,384 context tokens.
  The five optional terminal API cases passed separately in offline
  containers with the cached terminal dependencies. All **181 distinct
  cases** passed across the runs.
- The 34 focused command and Docker cases passed on both Compose 2.26.1
  and 2.40.3. Follow-up runs verified that stopping one version leaves a
  second one running, and that the new terminal starts after scoped legacy
  shutdown. The passing logs contained only unittest output. Failure and
  timeout fixtures verified command diagnostics from both output streams.
- The five documentation checks passed using the temporary editable
  environment. Checked fixture cleanup and used cached images.

## 2026-11-22 12:13 CET — Documentation audit and full verification

- On Debian 13.8 in WSL2 with Python 3.13.5, an offline editable install
  from cached build tools ran **150 tests with 135 passes and 15 skips**.
  The original checkout virtualenv points to an inaccessible interpreter.
  A temporary virtualenv used system Python and the checkout's cached pip.
  The five documentation checks passed independently.
- With Docker Engine 26.1.5 and Debian Compose 2.26.1-4, the Docker and
  Open Terminal run passed **143 of 150 tests**. The seven skips were the
  GPU and catalog tests and five optional terminal API tests. The Open
  Terminal fixture passed with the production addressless bridge on both
  serving profiles. Its separate backup fixture verified workspace ownership
  and key recovery. Four provisioner API checks passed in the cached image
  with system Python, and the timeout test passed with the image's
  Python 3.12 and pinned Open Terminal package.
- The isolated GPU workflow passed in 451 seconds on the RTX 4070 Ti
  SUPER. It downloaded Ministral, verified offline inference at 16,384
  context tokens with full GPU placement, and checked import recovery,
  connected serving and a 2.8 GiB model export/import round trip.
- The public catalog test passed for all five documented model specifiers.
  An isolated stop fixture confirmed that `loaf stop -n` keeps the stopped
  container and default `loaf stop` removes it. The same test marker was
  readable from the named volume after recreating the service.
- A checksum-verified, temporary Compose 2.40.3 plugin passed all six
  Docker contract tests. Live preflight passed with both Compose versions,
  with a Compose warning on 2.26.1-4 and `OK` on 2.40.3.
- Built and installed a fresh wheel offline in a second virtualenv.
  Checked the packaged project link, Continue and Tether commands, status
  and both stop plans. The generated systemd unit passed `systemd-analyze verify`.
  Docker coverage used prebuilt images. Systemd and Tether coverage ended
  at generated-unit validation and CLI output, respectively.

## 2026-11-22 11:58 CET — Grouped configuration and Open Terminal image checks

- On system Python 3.13.5, the source-based suite ran 132 checks, with
  117 passes and 15 opt-in skips. Installed-package tests were excluded.
  All 7 focused readiness and documentation checks passed.
- Live preflight exited 0 and displayed the bearer key and Open Terminal
  image under their new headings. A simulated missing image returned exit 1.

## 2026-11-22 11:52 CET — Short flag for keeping stopped containers

- All 50 launcher and documentation tests passed with zero skips under
  system Python 3.13.5. Dry-run coverage confirmed that `-n` keeps containers.

## 2026-11-22 11:52 CET — Remove service containers by default

- On system Python 3.13.5, the source-based suite excluding installed-package
  tests ran 133 checks with 118 passes and 15 opt-in skips. All 5 documentation
  checks passed separately. Dry-run coverage verified default removal and its
  opt-out without stopping the live deployment.

## 2026-11-22 11:51 CET — Compose compatibility warning

- All 8 readiness and documentation checks passed with zero skips.
- Live status on Debian with Compose 2.26.1-4 printed `WARN` for Compose,
  passed preflight and returned exit code 0.

## 2026-11-22 11:50 CET — Optional container removal

- On system Python 3.13.5, all 50 launcher and documentation tests passed
  with no skips. An initial run with a relative `PYTHONPATH` failed three
  subprocess imports and passed after switching to an absolute path.
- Verbose dry runs verified the stop and removal order and profile selection.
  The live deployment was left running.

## 2026-11-22 11:48 CET — Docker Engine and Open Terminal status

- The source-based suite ran 132 checks with 117 passes and 15 opt-in
  skips, excluding installed-package tests. All 8 readiness and documentation
  checks passed in a focused run.
- Live status reported `WARN` for Engine 26.1.5 and `OK` for healthy Open Terminal.
  The command exited 1 because the host's Compose 2.26.1 is below the existing
  minimum of 2.30.

## 2026-11-22 11:44 CET — Ollama model catalog readiness

- The read-only probe against the local `ollama` volume returned
  `readable; local model data found`. Two readiness checks and five documentation
  checks passed under system Python 3.13.5 with no skips.

## 2026-11-22 11:39 CET — Status report layout

- On Debian with system Python 3.13.5, the source-based suite ran 130 checks
  with 115 passes and 15 opt-in skips. All 5 documentation checks passed
  separately. The suite excluded installed-package tests and used mocked Docker
  calls to verify status output and readiness exit codes.

## 2026-11-22 11:32 CET — Tether installation command

- On Debian with system Python 3.13.5, all 4 integration checks and all 5
  documentation checks passed with zero skips. A CLI smoke check printed the
  requested URL and installation steps. Desktop installation was outside the test scope.

## 2026-11-22 11:18 CET — Host integration commands and Tether

- On Debian with system Python 3.13.5, all 5 documentation checks passed.
  The source-based suite excluding installed-package tests ran 129 checks
  with 114 passes and 15 skips for opt-in Docker, terminal, GPU and catalog checks.
- The first system-Python run reported missing installed entry points and
  editable metadata, plus module import failures from other directories.
  The checkout virtualenv interpreter was inaccessible, and the system
  Python lacked ensurepip for making an alternate installation.
- Checked Tether installer names and connection steps against the publisher's
  documentation. Desktop installation and sign-in were untested.

## 2026-11-22 01:27 CET — Terminal backup and APT source files

- On Debian 13.8 in WSL2 with Docker Engine 26.1.5 and Compose 2.26.1-4,
  the Docker and terminal test suite ran **146 checks with 144 passes**.
  The GPU and public model catalog checks were opted out.
- The disposable backup round trip restored the versioned Open Terminal
  image, a Home file with its UID and GID, the volume's ownership labels,
  and a bearer key with mode `0600`. Regression checks covered older
  archives, a tampered terminal payload, and an existing different key.
- Compose builds against a loopback mirror verified both `.list` and
  `.sources` overrides and preservation of inherited sources when the
  setting was blank. These builds started from the cached terminal image
  with its packages already installed. GCC and Sphinx ran with networking
  disabled.
- All **5 documentation checks** passed using an alternate virtualenv
  because the checkout interpreter could not execute. An offline wheel
  build included the default APT source placeholder.
  [Terminal server checks](verification.md#terminal-server-checks)

## 2026-11-22 00:58 CET — Terminal server rename and extended checks

- On Debian 13.8 in WSL2 with Docker Engine 26.1.5 and Compose 2.26.1-4,
  `loaf test -v` ran **141 checks with 140 passes**. Docker, the public model
  catalog and production terminal isolation were enabled. The GPU test was
  skipped in this run and passed in a separate **12-test** Docker/GPU/catalog
  run. That run covered Ministral inference and live import with
  `--delete` using the Ollama v2 archive.
- Both Open WebUI profiles verified the new terminal URL and used the saved
  older URL before and after a cold restart on internal networks. The
  GCC/G++, Python and Sphinx workflows, workspace isolation and persistent
  homes passed. The test-only legacy bridge option passed separately in
  both profiles. A disposable migration check stopped only the old service
  in the selected Compose project.
- All **5 documentation checks** passed with the alternate verification
  virtualenv because the checkout interpreter could not execute. A wheel
  built with package indexes disabled contained the renamed Docker context
  and Compose service.
- Disconnection was exercised by removing the fixture's network attachment.
  Physical disconnection, native Debian and Docker Engine 28+ were outside
  this run. [Terminal server checks](verification.md#terminal-server-checks)

## 2026-11-22 00:42 CET — Default workspace Home

- On Debian 13 in WSL2 with Docker Engine 26.1.5, the suite with Docker and
  tool-server checks enabled passed **138 tests with 2 skips**. GPU and
  catalog checks remained opted out. All **5 documentation checks passed**.
- Both disposable Open WebUI profiles reported writable Home before and
  after restart. Two chats under one account started in Home, saw the
  same files, and could select working folders independently.
- The live runner reported `/home/user` and the **Home** breadcrumb for a
  fresh session. [Tool server setup](terminal-server.md#use-the-terminal-from-a-model)

## 2026-11-22 00:09 CET — Ministral with Open Terminal

- On Debian 13 in WSL2, a disposable Open WebUI v0.13.4 deployment used
  the installed `Ministral-3B-Reasoning:latest` through local Ollama and
  the real workspace provisioner. The model called `run_command` and
  received `Hello, World!` in the tool output. The archive contained
  the generated C source and GCC executable.
- A broader prompt produced valid tool calls but invalid C source in the
  first run. The documented prompt supplied one command and verified
  source creation, compilation, and execution. All **5 documentation
  checks passed**.
  [Tested prompt](terminal-server.md#use-the-terminal-from-a-model)

## 2026-11-21 23:59 CET — Ollama v2 model archives

- On Debian 13 in WSL2, the focused GPU workflow passed in **486 seconds**
  with Ollama 0.40.17. It verified 16,384-token GPU inference, restored
  registrations, exported a 2.8 GiB model snapshot, and imported it with
  `--delete` while keeping the serving containers running.
- A small disposable Ollama store also preserved real `manifests-v2` links
  across export and import and removed a destination-only tag. Regression
  coverage rejects escaping links, missing targets and layers,
  mismatched manifest hashes, and files nested beneath tag links.
- The separate suite with ordinary Docker checks passed **137 tests with
  3 skips**. The GPU test ran separately. The model catalog and tool-server
  acceptance checks remained opted out for this run. All **5 documentation
  checks passed**. [Model transfers](transfer.md#transfer-models-between-existing-deployments)

## 2026-11-21 23:48 CET — Modelfile checks during backup

- On Debian 13 in WSL2, the suite with Docker checks enabled passed
  **136 tests with 3 skips**. The GPU, model catalog, and full tool-server
  acceptance checks remained opted out. All **5 documentation checks passed**.
- Backup regression tests confirmed that a stale recipe, missing blob,
  and failed inspection produce warnings with a successful backup. The
  stored volume payload matched the captured bytes. The status test used
  its own configuration directory.
- A disposable Ollama 0.40.17 container with networking disabled and a
  read-only model volume accepted a matching generated Modelfile and
  reported the recipe whose temperature differed from the registered model.
  [Modelfile checks](transfer.md#check-saved-modelfiles-during-backup)

## 2026-11-21 23:15 CET — Production tool server on Docker Engine 26+

- On Debian 13 in WSL2 with Docker Engine 26.1.5 and Compose 2.26.1,
  the rebuilt tool image passed both Open WebUI profiles and their cold
  restarts with the production entrypoint. The acceptance fixture removed
  the online profile's extra network before restarting.
- The host bridge checks found no IPv4 addresses. Runners had no default
  route or IPv6 addresses and could reach neither each other nor the
  provisioner's control API. The software and documentation workflows
  passed in the same workspaces.
- The suite passed **135 tests with 2 skips**. The GPU and catalog tests
  remained opted out. All **5 documentation checks passed**. Native Debian
  and the Engine 28+ integration path remain to be verified.
  [Tool server acceptance checks](terminal-server.md#acceptance-checks)

## 2026-11-21 23:02 CET — Tool-server tests on older Docker Engines

- On Debian 13 in WSL2 with Docker Engine 26.1.5 and Compose 2.26.1,
  `loaf test` passed **135 tests with 2 skips** with Docker, tool-server,
  and legacy bridge options enabled through a temporary INI. The GPU
  and catalog tests remained opted out. All **5 documentation checks passed**.
- Both Open WebUI profiles passed with the real provisioner and separate
  ordinary internal bridges. The test verified access grants, per-user
  workspace persistence, GCC/G++ builds, pytest, and Sphinx
  artifact uploads and downloads, and cold restart. It also removed the
  online profile's extra network attachment before restarting.
- With the legacy option off, the acceptance test skipped on Engine 26
  and reported the INI setting. The Engine 28+ isolated-bridge integration
  and a native Debian run remain outstanding.
  [Tool server acceptance checks](terminal-server.md#acceptance-checks)

## 2026-11-21 22:53 CET — Open WebUI tool workflows

- On Debian 13 in WSL2, the installed suite passed **133 tests with 3
  skips** with Docker and tool-server checks enabled. Compose 2.26.1
  rendered the NVIDIA device reservation. The Compose contracts, archive
  round trip, and disposable shell checks passed. All **5 documentation
  checks passed**. The GPU and catalog checks remained opted out.
- A non-root Open Terminal container with Docker networking disabled
  passed the GCC/G++ build, Python edit and pytest, CTest, Sphinx revision
  build, file upload, ZIP download, and command timeout.
- The local Open WebUI image also passed the two-account workflows, denied
  access check, and cold restart on an internal bridge. The smoke test
  used the production HTTP proxy with a temporary local-process workspace
  manager.
- The full Docker workspace test skipped on Docker Engine 26.1.5.
  Engine 28+ is needed for per-user isolated bridge provisioning. The full
  acceptance run remains outstanding on native Debian and Debian in WSL2.
  [Tool server acceptance checks](terminal-server.md#acceptance-checks)

## 2026-11-21 22:38 CET — Optional per-user tool server

- The installed suite passed **127 tests with 9 skips**, including the
  loopback HTTP and WebSocket proxy tests and a real command timeout
  through Open Terminal 0.17.20. All **5 documentation checks passed**.
- Built the shared Docker image. A disposable non-root runner with
  networking disabled passed the Python, C compiler, CMake, Ninja, Sphinx,
  workspace-write, and command-timeout smoke checks. The launcher wheel
  contained the tool server code and Docker build files.
- The opted-in provisioner Docker test was skipped in the suite.
  This host has Docker Engine 26.1.5 and Compose 2.26.1. Isolated per-user
  network provisioning and full Open WebUI workflows on the target hosts
  still require a run on the documented Docker and Compose versions.

## 2026-11-21 22:17 CET — Tool server setup guide

- All **5 documentation checks passed** using the temporary editable
  installation.

## 2026-11-21 22:15 CET — Open Terminal design documentation

- All **5 documentation checks passed** using the temporary editable
  installation. The change records a design decision. Deployment checks
  remain pending.

## 2026-11-21 22:06 CET — Per-user terminal research

- Reviewed the tagged Open WebUI terminal proxy and tool-discovery source,
  current Terminals license and release notes, and community implementation
  sources. The proposed Docker integration was evaluated at the interface
  level. Native Debian and WSL2 runtime tests remain pending.
- All **5 documentation checks passed** using the temporary editable
  installation because the checkout interpreter was inaccessible.

## 2026-11-21 21:53 CET — Tool server proposal

- All **5 documentation checks passed** using a temporary editable
  installation. The checkout virtualenv's interpreter was inaccessible.
- Scope was documentation and upstream source review. Deployment, license
  inventory for pinned images, and native Debian and WSL2 runtime checks
  remain part of the proposed evaluation.

## 2026-11-21 21:42 CET — Model transfers and readiness

- The installed suite on Debian with Python 3.13 had **120 passed and 8
  skipped** with Docker, GPU and catalog opt-ins disabled. Regression checks
  used real `rsync` for model merge and deletion, simulated Docker readiness
  checks and validated backup payload hashes before any Docker calls.
- The documentation suite passed all **5 checks**. Both suites used a
  temporary editable installation because the checkout's virtualenv points
  to an inaccessible interpreter.
- A local `loaf status` run reported Compose 2.26.1-4 and a blocked Docker
  socket. Live transfer with serving containers and GPU access remain
  unverified on this host.

## 2026-11-21 18:22 CET — NVIDIA installer cache

- Setup fetched the four NVIDIA Toolkit 1.20.1 installers into
  `python-package/deb/` and reused them on the next run. All four matched
  byte for byte after checkout archive recovery.
- Debian containers installed the recovered packages with networking disabled.
  The offline setup path used the recovered cache, and APT reported 0 bytes
  to fetch. `nvidia-ctk --version` reported 1.20.1.
- The installed suite had **116 passed and 8 skipped** with Docker, GPU and
  catalog opt-ins disabled.

## 2026-11-21 18:15 CET — Foreground log following

- A disposable Compose project streamed logs after startup. Ctrl+C ended log
  following with both containers still running, and `loaf stop` stopped them
  while another command followed logs. The generated unit passed
  `systemd-analyze verify`.
- The installed suite had **114 passed and 8 skipped** with Docker, GPU and
  catalog opt-ins disabled. Dry-run tests checked log scopes for Compose and
  full transfer commands.

## 2026-11-21 18:07 CET — Generated host integrations

- `systemd-analyze verify` accepted the generated unit with spaces, percent
  signs and dollar signs in paths. The printed start and stop commands reached
  the installed launcher and selected the same deployment in dry runs.
- Parsed generated Continue YAML for the three bundled 8 GB models and checked
  the API URL, placeholders and context providers. The installed suite had
  **112 passed and 8 skipped** with Docker, GPU and catalog opt-ins disabled.

## 2026-11-21 17:59 CET — Local configuration overrides

- Verified that the starter files match the packaged values. A partial local
  `config.ini` inherited packaged settings, and a local model list replaced the
  bundled list. A local setting also selected a packaged model example.
- Local edits reused the cached wheel. A separate wheel installation used the
  packaged defaults, and a checkout archive included both sets of files.
- The installed suite on Debian with Python 3.13 had **110 passed and 8
  skipped**, with Docker, GPU and catalog opt-ins disabled.

## 2026-11-21 17:47 CET — Checkout configuration

- Built wheels from a disposable checkout and from a source distribution.
  Both included all six configuration files from `config/` with matching
  contents. The wheel installation and recovered checkout resolved the settings
  and model examples through their deployment paths.
- Ran the installed suite on Debian with Python 3.13. **108 passed and 8
  skipped**, with Docker, GPU and catalog opt-ins disabled.

## 2026-11-21 17:41 CET — Packaged deployment data

- On Debian with Python 3.13, installed the editable launcher in a disposable
  checkout. The fast suite had **108 passed and 8 skipped** with Docker, GPU and
  catalog opt-ins disabled. The checkout archive included the packaged data
  and excluded the generated link.
- Checked all 15 deployment files in the wheel. Installed it separately,
  created a link outside the checkout and used the bundled model list. Compose
  2.40.3 resolved all three contexts from inside the link. Three Compose
  configuration tests and the disposable Ollama script fixture passed separately.

## 2026-11-21 17:32 CET — Docker build context relocation

- Compose 2.40.3 discovered the root `compose.yml` in a disposable checkout
  copy and resolved all three build contexts under `docker/`. The default
  volume names resolved without namespace warnings.
- Three Compose configuration tests and the disposable Ollama script fixture
  passed. A source-based run had **90 passed and 8 skipped**. The package
  entry-point suite and image rebuilds were outside this run.

## 2026-11-21 17:13 CET — Test suite relocation

- On Debian 13, the installed `loaf test` found the suite both from the checkout
  root and from `/tmp` with `--project-dir`. Each run had **105 passed and 8
  skipped** with Docker, GPU and catalog opt-ins disabled. Direct documentation
  checks passed from `python-package/`.
- The launcher wheel excluded the relocated tests and shell fixtures. Both Docker
  fixture tests passed separately using the cached image and disposable resources.

## 2026-11-21 17:07 CET — Wheel reuse and setup output

- Bootstrapped with system Python in a disposable Debian 13 checkout and
  reran `loaf setup` with package indexes disabled. The second run reused the
  wheel and the editable installation. Project archiving reused the same wheel
  and included the cache record. The toolkit executable was simulated.
- A missing build requirement surfaced pip's diagnostic. Successful builds and
  installs showed status messages. The installed suite had **105 passed and
  8 skipped**, with Docker, GPU and catalog opt-ins disabled.

## 2026-11-21 17:00 CET — Command output and progress

- Ran the installed `loaf test` in a disposable Debian 13 checkout. **103 passed,
  8 skipped.** Docker, GPU and catalog integration checks were disabled.
- A simulated Docker client confirmed that an 11-second startup reports
  elapsed time once, preserves Compose output and prints the WebUI address on
  success. The `status` table was unchanged. Live and dry runs showed command
  traces with `-v` and plain-language steps by default.

## 2026-11-21 16:53 CET — Installation-only setup

- Ran `loaf setup` from system Python in a fresh Debian 13 checkout without
  Docker installed and with the model selection file temporarily removed.
  It prepared `.venv`, the Python wheel cache and the NVIDIA toolkit packages.
- The fast suite passed **101 tests** with **8 skipped**. Docker, GPU and catalog
  opt-ins were disabled. Setup tests covered checkout locking, dry-run behavior,
  installer errors and the absence of Docker calls.

## 2026-11-21 16:49 CET — Quick Setup

- Ran the one-command setup from system Python in a disposable Debian 13
  checkout. It installed the launcher, cached wheels and prepared the NVIDIA
  toolkit packages. A simulated Docker client verified the stage order through
  offline startup without downloading models or starting the stack.
- The fast suite ran **109 tests with 101 passed and 8 skipped**. The Docker,
  GPU and catalog opt-ins were disabled. The suite checked stage failure,
  checkout locking, dry-run behavior and the active Bash documentation.

## 2026-11-21 16:42 CET — Fresh checkout and model-only transfer

- Started with a clean source checkout and system Python in a disposable
  Debian container. `loaf install` prepared `.venv`, the wheel cache and
  the NVIDIA Toolkit installer packages. The installed launcher ran on
  the test host from the same checkout.
- `loaf test -v` passed **107 tests with zero skips** in 343.209 seconds.
  Docker, GPU and catalog checks ran with Compose 2.40.3 and PowerShell 7.5.4.
  The GPU fixture downloaded its own model, verified the display alias and
  16,384-token inference at 100% GPU, and exercised import recovery and
  connected WebUI health.
- A separate Docker test backed up and restored model data across two
  disposable namespaces. The `rsync` merge updated manifests and Modelfiles,
  copied the missing blob, and preserved a destination-only model.

## 2026-11-21 14:07 CET — Python wheel export and recovery

- The local suite passed **95 tests** with **9 skipped**. Docker, GPU and
  catalog checks were disabled, and PowerShell was unavailable.
- A disposable Debian Python 3.13 checkout downloaded Setuptools from PyPI.
  Installation also resolved direct and transitive fixture dependencies from
  local wheels. Backup rebuilt a changed launcher wheel and archived the full
  wheel directory. The recovered wheels passed editable and regular installs
  with package indexes disabled.
- The round trip used preseeded pip, skipped the NVIDIA Toolkit step, and
  simulated Docker payloads. A separate test checks that a virtualenv with
  a missing base Python needs repair.

## 2026-11-21 13:47 CET — INI model selection

- The local suite passed **91 tests** with **9 skipped**. Enabling the public
  manifest checks gave **92 passed** with **8 skipped**. Docker and GPU checks
  were disabled, and PowerShell was unavailable.
- CLI dry runs confirmed the default INI and the explicit text-list override.
  Tests covered custom INIs for download, export, and backup, including errors
  before Docker starts when a selected file is missing or invalid.

## 2026-11-21 13:39 CET — WebUI authentication

- The Docker-free suite passed **86 tests** with **9 skipped**. With Docker
  and temporary Compose 2.40.3, **92 passed** with **3 skipped**. The GPU and
  catalog checks were disabled, and PowerShell was unavailable.
- Compose resolved `WEBUI_AUTH=true` by default and `false` with the INI
  override in both serving profiles. A disposable offline deployment of
  WebUI 0.13.4 reported both values through `/api/config` before any user
  registration. The disposable resources were removed.

## 2026-11-21 13:33 CET — Project INI configuration

- The Docker-free suite passed **86 tests** with **9 skipped**, including a run
  from a project with custom INI settings.
- Docker and catalog checks with temporary Compose 2.40.3 passed **93 tests**
  with **2 skipped**. PowerShell was unavailable. The full GPU inference test
  was skipped while a live Ollama container was running.
- A disposable `loaf offline` deployment passed WebUI health on a custom
  loopback port. Docker reported the configured 8192-token context, GPU ID,
  and internal Ollama network. The disposable resources were removed.

## 2026-11-21 13:06 CET — Debian installer and full Docker transfer

- The Docker and catalog regression suite passed **85 tests** with **2 skipped**
  for PowerShell and the separate GPU fixture. The GPU workflow below ran
  independently.
- Disposable Debian containers covered APT-managed Python with missing venv
  support, the signed NVIDIA repository, staged packages, repeated installs,
  and virtualenv relocation. A non-APT Python install using prepared wheels
  and toolkit packages also passed with networking disabled.
- Real Docker on Debian in WSL2 passed `loaf build`, `loaf upgrade`,
  `loaf backup --export`, `loaf restore`, `loaf online` and `loaf offline` in
  a disposable namespace. Used temporary Compose 2.40.3 and Buildx 0.20.1,
  with existing base images served from a local registry for the upgrade.
  The recovered checkout contained its Git history, `.venv` and four matching
  toolkit packages. Connected WebUI was healthy, and restored offline
  Ministral inference reported **100% GPU** on the RTX 4070 Ti SUPER.
  Compose emitted ownership-label warnings for volumes created by restore.

## 2026-11-21 12:34 CET — Versioned deployments

- `loaf test` with Docker and catalog checks enabled: **78 passed, 2 skipped**.
  The GPU workflow was disabled and PowerShell was unavailable.
- Temporary Compose 2.40.3 resolved the versioned project, images, volumes
  and networks. Namespace checks also passed against a real Docker save
  archive with disposable tags.

## 2026-11-21 12:29 CET — Repository and verification cleanup

- Full `loaf test -v`: **77 passed, 1 skipped** in 223.607 seconds with
  Docker, GPU and catalog checks enabled. PowerShell was unavailable.
- Temporary Compose 2.40.3 and an RTX 4070 Ti SUPER backed the isolated
  GPU run. Cached Ministral passed offline inference at 16,384 context
  tokens with full GPU placement. Import recovery and connected WebUI
  health passed. Cleanup removed the disposable resources.
- The updated shell scripts passed in a disposable container. The production
  volume commands passed a Docker round trip with synthetic data. Python,
  Bash, integration YAML and the systemd unit passed static checks.

## 2026-11-21 11:39 CET — Transfer and first-run fixes

- Default suite: **70 passed, 7 skipped**. Tests covered legacy archive
  exclusion, export and import ordering, dry runs, and failure handling.
- **3 Docker checks passed** using local images and disposable fixtures.
  Compose 2.26.1 is below the required 2.30, so the Compose and GPU workflow
  checks were skipped.
- A fresh Debian editable install from local wheels passed with both direct
  invocation and virtualenv activation. PowerShell parsing and catalog checks
  were skipped.

## 2026-11-21 11:28 CET — Documentation audit

- Checked the guides against the launcher, Compose configuration, scripts and
  test sources. Confirmed that the test account was in the Debian `docker` group.
- Docker-free suite: **67 passed, 7 skipped, 1 failed**. Documentation checks
  passed where parsers were available. The existing legacy archive regression
  reproduced. Docker, GPU and catalog integration checks were skipped.

## 2026-11-21 11:16 CET — Local suite

- `loaf test` ran 75 tests. **67 passed, 7 skipped, 1 failed.**
- The transfer test found a legacy `offline-images.tar` inside `project.tar`.
  Docker, GPU and catalog checks were disabled. The PowerShell parser was unavailable.

## 2026-11-16 22:52 CET — Windows GPU and packaging checks

The time marks the Git commit (`eb2809c`) that recorded these checks. The
original record gives the run date and durations, with no execution times.

- **Full suite:** 65 passed in 265.091 seconds with Docker, GPU and catalog
  checks enabled. Windows 11, Docker Desktop/WSL2, Python 3.14.2, RTX 4070 Ti
  SUPER (16 GB), Ollama 0.40.16 and Open WebUI 0.13.3.
- **Offline GPU:** Ministral completed a prompt above 7008 tokens at 16,384
  context tokens with 100% GPU placement. Explicit import rebuilt the model
  registration and recovered after an invalid export was removed.
- **Fast suite and packaging:** 59 passed and 6 skipped in 18.825 seconds.
  Offline editable and wheel installs passed CLI checks, including project
  paths with spaces, and `pip check` passed.
- **Earlier checks:** 52 tests passed in 164.587 seconds after source comments
  were added. Source comparisons and resolved Compose configuration confirmed
  no behavior changes. Saved launcher plans also matched their baselines.
