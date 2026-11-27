# Contributing

> ℹ️ **INFO**\
> Tell me if you want to contribute so we can coordinate efforts - of course
> I value your effort and time - so I will reject unsolicited pull requests.
> If you make an PR for the following file, I will let the agents work on it:\
> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; `docs/instructions/development.md`\
> Later, when not so many things change, I might be open normal PRs.

`loaf` prepares, serves and transfers Ollama and Open WebUI deployments on
Debian. Start with the [repository guide](AGENTS.md) and the
[architecture specification](docs/instructions/architecture.md).

## Report a problem

For ordinary bugs, open an issue in the GitHub repository. Include the checkout
revision, the exact command, the expected and observed behavior, and relevant
Python, Docker Engine and Compose versions. A redacted excerpt of `loaf status`
or the failing command's output can help reproduce the issue. Keep credentials,
private paths, chats, workspace files and deployment archives out of reports.
Use the [security policy](SECURITY.md) for vulnerabilities.

## Verify a change

Use a Python virtual environment to install the launcher in editable form and
run the Docker-free checks from the checkout root.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --editable ./python-package
.venv/bin/loaf test -v
```

GitHub's Verify workflow runs these checks on Python 3.10 and 3.13 and tests
a wheel installation. The [verification guide](docs/verification.md) shows
how to run selected cases and opt into Docker, GPU, Open Terminal or catalog
checks.

Keep related code, tests and guide updates together. In the pull request,
describe behavior changes and list the verification you ran, including skips.
Add dated entries to the [development record](docs/development-record.md)
and [verification record](docs/verification-record.md) in newest-first order.
Leave existing entries intact.
