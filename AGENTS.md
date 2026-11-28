# Repository guide

This checkout packages Local AI Ferry: its Python CLI `loaf` prepares, serves and transfers
Ollama models and Open WebUI with Docker Compose. Start with [README.md](README.md).

## Development style

The authors are to be considered the system engineer who decide all relevant architectural
decisions.
The agents are the knowledgeable developers who shall lay focus on the following development
goals before any functionality:
- no confusing or misleading CLI -> intuitive design and naming
- correctness and safety of code regarding sensitive offline environments
- reduced complexity and simplicity while keeping full functionality
- understandability of code before ressource effectiveness - if ressources are not central

Assume the markdown files under `docs/instructions` to be the design specification.
If a request from the author violates one statement there, please ask how to proceed.
All other files in `docs/` directly are to be updated by the agent.

Put all development iteration descriptions into dated entries with newest-first ordering into
`docs/development-record.md`.
All relevant test runs shall be recorded into `docs/verification-record.md`.

## Working rules

- Never read `python-package/src/loaf/cli.py` whole. Search it with Grep or `grep -n`,
  or read only the lines you need with an offset and limit.
- Check claims against the code: the CLI in [cli.py](python-package/src/loaf/cli.py), setup in
  [setup.py](python-package/src/loaf/setup.py), archives in [transfer.py](python-package/src/loaf/transfer.py),
  defaults in the [packaged config.ini](python-package/src/loaf/data/config/config.ini),
  [config.py](python-package/src/loaf/config.py), [compose.yml](python-package/src/loaf/data/compose.yml) and the
  [Dockerfiles](python-package/src/loaf/data/docker/), behavior and coverage in [the tests](python-package/tests/).
- Explain each behavior in one place and link to it from elsewhere: commands and options in
  [docs/cli-help.md](docs/cli-help.md), every other topic in the guide that the map below names.
  Change that guide and its examples together with the behavior.
- Record verification with timestamp, timezone, scope, outcome and skips, and keep implementation,
  expected behavior and observed results apart. Check changing upstream claims against the
  official project or publisher.
- Run the documentation checks from `python-package/` with
  `../.venv/bin/python -m unittest tests.test_documentation`; the verification guide lists the
  other suites. Respect session instructions about running Docker or interfering with active builds.

## Documentation map

| Topic | Owner |
| --- | --- |
| Commands and options | [CLI reference](docs/cli-help.md) |
| Design, profiles, networking and storage | [Architecture](docs/instructions/architecture.md) |
| Settings, defaults and override order | [Configuration](docs/configuration.md), [working](config/config.ini) and [packaged](python-package/src/loaf/data/config/config.ini) `config.ini` |
| Package installation, model selection and direct Compose use | [Launcher](docs/launcher.md) |
| Model choices and context estimates | [Models](docs/models.md), [working](config/models.ini) and [packaged](python-package/src/loaf/data/config/models.ini) `models.ini` |
| Host setup, offline installation and the Debian package | [Debian](docs/debian.md), [Installation](docs/installation.md), [package sources](packaging/debian/) |
| Open Terminal workspaces | [Terminal server](docs/terminal-server.md) |
| Archives, backup and restore | [Transfer](docs/transfer.md) |
| Release builds and the standalone executable | [Releases](docs/releases.md) |
| Operational problems | [Troubleshooting](docs/troubleshooting.md) |
| Test suites and coverage; dated results | [Verification](docs/verification.md), [verification record](docs/verification-record.md) |
| Open tasks; dated milestones | [Development](docs/instructions/development.md), [development record](docs/development-record.md) |
