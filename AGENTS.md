# Repository guide

This checkout packages Local AI Ferry. Its Python CLI, `loaf`, prepares, serves
and transfers Ollama models and Open WebUI with Docker Compose. Start with
[README.md](README.md).

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


## Documentation map

| Find | Read |
| --- | --- |
| Implemented design, profiles, networking and storage | [Architecture](docs/instructions/architecture.md) |
| User settings, defaults and override order | [Working config.ini](config/config.ini), [Packaged defaults](python-package/src/loaf/data/config/config.ini), [Configuration](docs/configuration.md) |
| CLI commands, options and package installation | [Launcher](docs/launcher.md) |
| Host setup and offline installation | [Debian](docs/debian.md), [Installation](docs/installation.md) |
| Model selection, choices and context estimates | [Working selection](config/models.ini), [Packaged selection](python-package/src/loaf/data/config/models.ini), [Models](docs/models.md) |
| Docker build contexts and direct Compose use | [Deployment data](python-package/src/loaf/data/), [Link the Docker project](docs/launcher.md#link-the-docker-project) |
| Per-account Open Terminal setup, isolation and tests | [Terminal server](docs/terminal-server.md) |
| Archive format and recovery | [Transfer](docs/transfer.md) |
| Operational problems | [Troubleshooting](docs/troubleshooting.md) |
| Test commands, coverage and prerequisites | [Verification](docs/verification.md) |
| Timestamped test outcomes and environments | [Verification record](docs/verification-record.md) |
| Open tasks and proposed functionality | [Development](docs/instructions/development.md) |
| Dated implementation milestones | [Development record](docs/development-record.md) |

## Keep documentation current

- Check CLI claims against [cli.py](python-package/src/loaf/cli.py), installation
  claims against [setup.py](python-package/src/loaf/setup.py), archive claims
  against [transfer.py](python-package/src/loaf/transfer.py), and defaults
  against the [packaged config.ini](python-package/src/loaf/data/config/config.ini), [config.py](python-package/src/loaf/config.py),
  [compose.yml](python-package/src/loaf/data/compose.yml) and the [Dockerfiles](python-package/src/loaf/data/docker/).
  Check behavior and coverage against [python-package/tests/](python-package/tests/).
- Update the owning guide and affected examples in the same change as the
  behavior. Link to the guide from other pages and keep the README index current.
- Record actual verification with a timestamp, timezone, scope, outcome and
  skips. Keep implementation, expected behavior and observed results distinct.
  Check changing upstream claims against the official project or publisher.
- Run documentation checks from `python-package/` with
  `../.venv/bin/python -m unittest tests.test_documentation`. See the verification
  guide for the Docker-free suite and opt-in checks. Respect session instructions
  about running Docker or interfering with active builds.
