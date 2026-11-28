# cli.py - Cross-platform launcher behind the installed loaf command.
# Requires Python 3.10+ and Docker Compose; uses only the Python standard library.
# Keep planning separate from execution so dry runs and tests need no Docker daemon.
"""Local AI Ferry builds, runs and transfers local Ollama and Open WebUI deployments."""

from __future__ import annotations

import argparse
from configparser import ConfigParser, Error as INIError
from contextlib import nullcontext
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from typing import Iterator, Sequence
import zlib

from . import apt, assets, cleanup, completion, config, docs, integrations, readiness, resources, setup, transfer
from .locks import AlreadyRunningError, PROJECT_LOCK, project_lock
from .progress import wait_status


# --- Command names and model-list grammar ---
# One command vocabulary shared by argument parsing and the pure planner.
SERVICES = {
    "download": ("ollama-online",),
    "online": ("ollama-connected", "open-webui-online"),
    "offline": ("ollama-offline", "open-webui"),
}
PROFILES = tuple(SERVICES)
# The serving profiles that loaf run starts and loaf stop and loaf restart can select.
SERVING = ("offline", "online")
MODES = ("build", "upgrade", "download", "online", "offline", "status", "stop", "restart")
# The commands that run Compose; run selects a serving profile, the plan's mode.
COMPOSE_COMMANDS = ("build", "upgrade", "download", "run", "restart", "status", "stop")
TRANSFER_MODES = ("backup", "restore")
INSTALL_MODES = ("setup", "install")
# Package targets and the source checkout scripts that build them into dist/.
DIST_DIR = Path("dist")
# The Debian package carries the standalone executable, so both targets run the release build.
PACKAGE_SCRIPTS = {"binary": Path("scripts/build-release.sh"), "debian": Path("scripts/build-release.sh")}
# Tags may be quantization names or complete GGUF filenames. This conservative
# grammar checks syntax locally; repository/tag availability is a separate check.
MODEL_PATTERN = re.compile(
    r"hf\.co/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]*)?"
)
DEFAULT_MODEL_SECTION = "models.default"
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?")
TESTS_DIR = Path("python-package/tests")
# The terminal scripts that chat containers bind when an APT source replaces the image's sources.
TERMINAL_RUNTIME_DIR = ".loaf-terminal-runtime"
TERMINAL_RUNTIME_FILES = ("provisioner.py", "runner.py", "no-apt-source.list")
# The source that the launcher writes for [terminal_server] apt_repository_dir, named after the setting.
REPOSITORY_SOURCE = "apt_repository_dir.list"
APT_BUILD_SECRETS = {"LOAF_TERMINAL_BUILD_APT_SOURCE", "LOAF_TERMINAL_BUILD_APT_REPOSITORY_SOURCE"}
# Chat containers and builds with --with-apt-repos mount apt_repository_dir here.
APT_REPOSITORY = str(apt.REPOSITORY)
# A flat repository has its index at the root, a suite repository below dists/.
FLAT_APT_INDEXES = ("Release", "InRelease", *(f"Packages{suffix}" for suffix in ("", ".gz", ".xz", ".bz2", ".lz4", ".zst")))
TEST_RUNNER = """\
from pathlib import Path
import sys
import unittest

write_badges = len(sys.argv) == 4
if write_badges:
    # Load the same launcher package and preserve discovery's import path.
    sys.path.insert(0, sys.argv[3])
    from loaf.badges import BadgeResult, update_badges
    from loaf.locks import AlreadyRunningError
    sys.path.remove(sys.argv[3])

tests_dir = Path(sys.argv[1])
suite = unittest.defaultTestLoader.discover(str(tests_dir), top_level_dir=str(tests_dir.parent))
if not suite.countTestCases():
    print(f"No tests discovered in {tests_dir}.", file=sys.stderr)
    sys.exit(5)
runner = unittest.TextTestRunner(
    verbosity=int(sys.argv[2]), warnings=None if sys.warnoptions else "default", buffer=True,
)
if write_badges:
    runner.resultclass = BadgeResult
result = runner.run(suite)
if write_badges:
    try:
        update_badges(tests_dir.parent.parent, result)
    except (OSError, UnicodeError, AlreadyRunningError) as exc:
        print(f"Could not update test badges: {exc}", file=sys.stderr)
        sys.exit(1)
sys.exit(not result.wasSuccessful())
"""


class ModelFileError(ValueError):
    """A model list could not be read or contains an invalid specifier."""

# --- Input parsing and human-readable command output ---
def display_command(arguments: Sequence[str]) -> str:
    """Render a copyable command for PowerShell on Windows or a POSIX shell."""
    if sys.platform == "win32":
        # PowerShell needs & before a quoted executable. Single quotes suppress
        # expansion; doubling an apostrophe preserves it inside a literal string.
        return "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
    return shlex.join(arguments)


@dataclass(frozen=True)
class Model:
    source: str
    name: str = ""


def ini_model_entries(path: Path, source: str) -> Iterator[tuple[str, Model]]:
    parser = ConfigParser(interpolation=None)
    try:
        parser.read_string(source, source=str(path))
    except INIError as exc:
        raise ModelFileError(f"Cannot parse model configuration {path}: {exc}") from exc
    if parser.defaults():
        raise ModelFileError(f"{path}: use [models.default] in place of [DEFAULT].")
    sections = parser.sections()
    for section in sections:
        prefix, _, name = section.partition(".")
        if prefix != "models" or not name.strip():
            raise ModelFileError(f"{path}: invalid section [{section}]. Use [models.<name>].")
    named = [section for section in sections if section != DEFAULT_MODEL_SECTION]
    for section in named or sections:
        values = parser[section]
        if "model" not in values or set(values) - {"model", "name"}:
            raise ModelFileError(f"{path}: [{section}] requires model and accepts an optional name.")
        name = values.get("name", "")
        if "name" in values and not MODEL_NAME.fullmatch(name):
            raise ModelFileError(f"{path}: [{section}] invalid Ollama name {name!r}.")
        yield f"[{section}]", Model(values["model"], name if not name or ':' in name else f"{name}:latest")


def read_models(path: Path) -> list[Model]:
    """Read the selected model INI in file order."""
    if path.suffix.lower() != ".ini":
        raise ModelFileError(f"{path}: use a model INI file with [models.<name>] sections.")
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ModelFileError(f"Cannot read model list {path}: {exc}") from exc
    models: dict[str, Model] = {}
    for location, model in ini_model_entries(path, source):
        if not MODEL_PATTERN.fullmatch(model.source):
            raise ModelFileError(
                f"{path}:{location}: invalid model specifier {model.source!r}. "
                "Use hf.co/owner/repository[:quantization]."
            )
        key = (model.name or model.source).lower()
        if key in models and models[key] != model:
            raise ModelFileError(f"{path}:{location}: duplicate model name {model.name or model.source!r}.")
        models[key] = model
    if not models:
        raise ModelFileError(f"{path}: no models selected; add an hf.co specifier.")
    return list(models.values())


def select_models_file(layers: Sequence[Path], override: Path | None, configured: Path | None) -> Path:
    """Use the highest configuration layer with the model INI, from the project down to the packaged list."""
    if override is not None:
        return override.absolute()
    name = configured or "models.ini"
    for directory in reversed(layers):
        candidate = directory / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return layers[-1] / name


# --- Pure workflow planning: immutable steps, no subprocesses or file writes ---
@dataclass(frozen=True)
class Step:
    profiles: tuple[str, ...]
    arguments: tuple[str, ...]
    description: str = field(default="", compare=False)


STOP_ALL = Step(PROFILES, ("stop",), "Stop all deployment services")
STOP_WITH_TERMINAL = Step((*PROFILES, "terminal"), STOP_ALL.arguments, STOP_ALL.description)
STOP_DOWNLOADER = Step(("download",), ("stop", *SERVICES["download"]), "Stop the model downloader")
VALIDATE = Step(PROFILES, ("config", "--quiet"), "Validate the Compose configuration")


def plan(
    mode: str, models: Sequence[Model] = (), *, wait_timeout: int = config.DEFAULT_WAIT_TIMEOUT,
    export_definitions: bool = False, import_definitions: bool = False,
    terminal_server: bool | None = None, remove_containers: bool = True, cpu: bool = False,
    serving: str | None = None, stop_terminal: bool = True,
) -> list[Step]:
    """Return a deterministic plan; all validation happens before Docker runs.

    stop takes the serving profile to stop, or None for every profile, and restart the profile to stop and start
    again. stop_terminal decides whether the terminal server stops with a selected profile; the caller keeps it
    for the other running profile.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}")
    if wait_timeout < 0:
        raise ValueError("The wait timeout must be 0, for no limit, or a positive number of seconds.")
    if mode == "download" and not models:
        raise ValueError("Select at least one model for download or Modelfile export.")
    profiles = PROFILES if terminal_server is None else (*PROFILES, "terminal")
    steps = [Step(profiles, VALIDATE.arguments, VALIDATE.description)]
    if mode in ("build", "upgrade"):
        # Only the services that own build definitions produce images.
        options = ("--pull",) if mode == "upgrade" else ()
        builders = (*SERVICES["download"], *SERVICES["offline"])
        if terminal_server:
            builders += ("terminal-server",)
        description = "Refresh base images and rebuild the local images" if options else "Build the local images"
        return steps + [Step(profiles, ("build", *options, *builders), description)]
    if mode == "stop" and serving is None:
        steps.append(STOP_ALL if terminal_server is None else STOP_WITH_TERMINAL)
        if remove_containers:
            steps.append(Step(profiles, ("rm", "--force"), "Remove stopped deployment containers"))
        return steps
    if mode in ("stop", "restart") and serving not in SERVING:
        raise ValueError(f"Unknown serving profile: {serving}")
    if mode == "stop":
        services = (*SERVICES[serving], *(("terminal-server",) if terminal_server and stop_terminal else ()))
        steps.append(Step(profiles, ("stop", *services), f"Stop {serving} serving"))
        if remove_containers:
            steps.append(Step(profiles, ("rm", "--force", *services), f"Remove the stopped {serving} containers"))
        return steps
    if mode == "status":
        return [Step(profiles, ("ps", "--all"), "List all deployment containers")]
    if mode == "restart":
        # Stop and remove the profile's containers, then start it like run: every service starts fresh.
        stop = plan("stop", terminal_server=terminal_server, serving=serving, stop_terminal=stop_terminal)
        return stop + plan(serving, wait_timeout=wait_timeout, terminal_server=terminal_server, cpu=cpu)[1:]

    profile = mode
    targets = ("ollama-offline",) if import_definitions else SERVICES[profile]
    others = tuple(service for name in PROFILES if name != profile for service in SERVICES[name])
    start_terminal = terminal_server and profile in ("online", "offline") and not import_definitions
    if terminal_server is not None and not start_terminal:
        others += ("terminal-server",)
    # Stop other volume writers before handing the store to a different profile.
    # Expose only Ollama when a full restore requests Modelfile replay.
    stop = Step(profiles, ("stop",) if import_definitions else ("stop", *others),
                STOP_ALL.description if import_definitions else "Stop services from other profiles")
    # Without --wait-timeout, Compose waits for readiness without a limit.
    startup = ("up", "-d", "--no-build", "--pull", "never", "--wait",
               *(("--wait-timeout", str(wait_timeout)) if wait_timeout else ()))
    wait = f"wait up to {wait_timeout}s for readiness" if wait_timeout else "wait for readiness"
    backend = "Ollama on CPU" if cpu else "Ollama"
    description = {
        "download": "Start the model downloader",
        "online": f"Start {backend} and Open WebUI with internet access",
        "offline": f"Start offline {backend} and Open WebUI",
    }[profile]
    if import_definitions:
        description = f"Start offline {backend} and import saved Modelfiles"
    steps += [stop, Step((profile,), startup + targets, f"{description} and {wait}")]
    if start_terminal:
        steps.append(Step(("terminal",), startup + ("terminal-server",), "Start the terminal server"))
    if mode == "download":
        exec_args = ("exec", "-T", *targets)
        pulled = set()
        # A display name replaces the long source name unless another entry selects the source unnamed.
        renamed = {model.source for model in models if model.name} - {model.source for model in models if not model.name}
        for model in models:
            if not export_definitions and model.source not in pulled:
                steps.append(Step((profile,), (*exec_args, "ollama", "pull", model.source), f"Download {model.source}"))
                pulled.add(model.source)
            # Download already gave a renamed source its display name; its long name is gone.
            if model.name and not (export_definitions and model.source in renamed):
                steps.append(Step(
                    (profile,), (*exec_args, "ollama", "cp", model.source, model.name),
                    f"Tag {model.source} as {model.name}",
                ))
            if export_definitions:
                name = model.name or model.source
                steps.append(Step((profile,), (*exec_args, "export-modelfiles", name), f"Export the Modelfile for {name}"))
        if not export_definitions:
            for source in dict.fromkeys(model.source for model in models if model.source in renamed):
                steps.append(Step((profile,), (*exec_args, "ollama", "rm", source),
                                  f"Remove the long name {source}; its display name keeps the model"))
    return steps


# --- Compose invocation and execution ---
class Compose:
    """An explicit Compose project; argument lists never pass through a shell."""

    def __init__(
        self,
        project_dir: Path | None = None,
        *,
        files: Sequence[Path] | None = None,
        project_name: str | None = None,
        namespace: str = config.DEPLOYMENT,
        settings: config.Settings | None = None,
        verbose: bool = False,
        build_apt_sources: bool = False,
    ) -> None:
        # Resolve cwd when called, not when the package is imported or installed.
        self.project_dir = (project_dir or Path.cwd()).resolve()
        # Tests inject a generated Compose file and a unique project name while
        # production keeps stable resource names regardless of the caller's cwd.
        self.files = tuple(p.resolve() for p in (files or (assets.DATA_DIR / "compose.yml",)))
        self.namespace = namespace
        self.project_name = project_name or namespace
        self.settings = settings or config.Settings()
        self.verbose = verbose
        # loaf build --with-apt-repos: the [terminal_server] APT settings also serve the terminal image's build.
        self.build_apt_sources = build_apt_sources

    def command(self, step: Step) -> list[str]:
        command = [
            "docker", "compose", "--project-directory", str(self.project_dir),
            "--project-name", self.project_name,
        ]
        if not self.settings.docker_env_enable:
            # An empty env file replaces the project's .env, whose variables would override the settings.
            command += ["--env-file", os.devnull]
        files = self.files
        if self.settings.terminal_server_enabled:
            # Open WebUI receives the bearer key to preset its terminal connection.
            files += (assets.DATA_DIR / "compose.terminal-webui.yml",)
        if self.uses_terminal_runtime(step):
            files += (assets.DATA_DIR / "compose.terminal-runtime.yml",)
            if self.settings.terminal_server_apt_source_file is not None:
                files += (assets.DATA_DIR / "compose.terminal-apt-source.yml",)
            if self.settings.terminal_server_apt_repository_dir is not None:
                files += (assets.DATA_DIR / "compose.terminal-apt-repository.yml",)
        for path in files:
            command += ["--file", str(path)]
        for profile in step.profiles:
            command += ["--profile", profile]
        # Steps also name services explicitly: enabling a profile alone must not
        # accidentally start another service during a mode transition.
        return command + list(step.arguments)

    def variables(self, step: Step | None = None) -> dict[str, str]:
        variables = {"LOAF_NAMESPACE": config.namespace_name(self.namespace), **self.settings.compose_variables()}
        compose_dir = self.files[0].parent
        if compose_dir != self.project_dir:
            # Relative build contexts start at the project directory; point them to the deployment files.
            variables["LOAF_DOCKER_DIR"] = str(compose_dir / "docker")
        if self.settings.cuda_visible_devices and "CUDA_VISIBLE_DEVICES" not in os.environ:
            variables["CUDA_VISIBLE_DEVICES"] = self.settings.cuda_visible_devices
        if self.settings.terminal_server_enabled:
            variables["LOAF_TERMINAL_SECRET_FILE"] = str(self.terminal_secret_file())
        if step is not None and "terminal-server" in step.arguments:
            variables.update(self.terminal_apt_variables(step))
        return variables

    def terminal_apt_variables(self, step: Step) -> dict[str, str]:
        """The [terminal_server] APT settings for a step: every start of the terminal server applies them, so a
        changed setting takes effect with loaf restart or loaf run. A build uses them only with --with-apt-repos."""
        if step.arguments[0] == "build":
            if not self.build_apt_sources:
                return {}
            source, repository = self.terminal_apt_inputs(serving=False)
            variables = {}
            if source is not None:
                variables.update(LOAF_TERMINAL_BUILD_APT_SOURCE=source.text,
                                 LOAF_TERMINAL_BUILD_APT_SOURCE_FORMAT=source.suffix[1:],
                                 LOAF_TERMINAL_BUILD_APT_SOURCE_HASH=sha256(source.text.encode()).hexdigest())
            if repository is not None:
                text = self.repository_source(repository, source)
                variables.update(LOAF_TERMINAL_BUILD_APT_REPOSITORY_DIR=str(repository),
                                 LOAF_TERMINAL_BUILD_APT_REPOSITORY_SOURCE=text,
                                 LOAF_TERMINAL_BUILD_APT_REPOSITORY_SOURCE_HASH=sha256(text.encode()).hexdigest())
            return variables
        if not self.uses_terminal_runtime(step):
            return {}
        source, repository = self.terminal_apt_inputs(serving=True)
        provisioner = assets.DATA_DIR / "docker/terminal-server/provisioner.py"
        variables = {"LOAF_TERMINAL_RUNTIME_DIR": str(self.terminal_runtime_dir()),
                     "LOAF_TERMINAL_PROVISIONER_HASH": setup.file_digest(provisioner)}
        if source is not None:
            variables.update(LOAF_TERMINAL_APT_SOURCE_FILE=str(source.path),
                             LOAF_TERMINAL_APT_SOURCE_FORMAT=source.suffix[1:],
                             LOAF_TERMINAL_APT_SOURCE_HASH=sha256(source.text.encode()).hexdigest())
        if repository is not None:
            variables.update(LOAF_TERMINAL_APT_REPOSITORY_DIR=str(repository),
                             LOAF_TERMINAL_APT_REPOSITORY_SOURCE_HASH=sha256(
                                 self.repository_source(repository, source).encode()).hexdigest())
        return variables

    def terminal_secret_file(self) -> Path:
        return config.terminal_secret_path(self.project_dir, self.settings.terminal_server_secret_file)

    def terminal_runtime_dir(self) -> Path:
        return self.project_dir / TERMINAL_RUNTIME_DIR

    def stage_terminal_runtime(self) -> None:
        """Copy the bundled terminal scripts to a project path that stays the same across loaf releases.

        Chat containers bind them from there, so a new release changes what they run, not their mounts.
        """
        target = self.terminal_runtime_dir()
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise OSError(f"{target} must be a local directory.")
        target.mkdir(exist_ok=True)
        files = {name: (assets.DATA_DIR / "docker/terminal-server" / name).read_bytes() for name in TERMINAL_RUNTIME_FILES}
        source, repository = self.terminal_apt_inputs(serving=True)
        if repository is not None:
            files[REPOSITORY_SOURCE] = self.repository_source(repository, source).encode()
        else:
            (target / REPOSITORY_SOURCE).unlink(missing_ok=True)
        for name, data in files.items():
            path = target / name
            if path.is_file() and not path.is_symlink() and path.read_bytes() == data:
                continue
            pending = target / f".{name}.{os.getpid()}"
            pending.write_bytes(data)
            pending.chmod(0o644)
            pending.replace(path)

    def uses_terminal_runtime(self, step: Step) -> bool:
        """Start the provisioner from this launcher so loaded images use the current policy."""
        return (self.settings.terminal_server_enabled and step.arguments[0] == "up"
                and "terminal-server" in step.arguments)

    def terminal_apt_source(self) -> apt.Source | None:
        configured = self.settings.terminal_server_apt_source_file
        if configured is None:
            return None
        source = (self.project_dir / configured.expanduser()).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Terminal server APT source file is missing: {source}")
        return apt.read_source(source, configured.suffix)

    def terminal_apt_inputs(self, *, serving: bool) -> tuple[apt.Source | None, Path | None]:
        source = self.terminal_apt_source()
        repository = self.terminal_apt_repository_dir()
        if serving and source is not None:
            source.check_network(self.settings.terminal_server_network, repository=repository is not None)
        return source, repository

    def repository_source(self, repository: Path, source: apt.Source | None = None) -> str:
        """The APT source for apt_repository_dir: its flat index and each suite below dists/.

        A repository without a signature is marked trusted. A configured binary entry covers its suite and components.
        """
        if source is None:
            source = self.terminal_apt_source()

        entries = [(repository, "./", [])] if any((repository / name).exists() for name in FLAT_APT_INDEXES) else []
        dists = repository / "dists"
        for suite in sorted(dists.iterdir()) if dists.is_dir() else ():
            release = next((suite / name for name in ("InRelease", "Release") if (suite / name).is_file()), None)
            if suite.is_symlink() or release is None or not re.fullmatch(r"[A-Za-z0-9._-]+", suite.name):
                continue
            components = next((line.split(":", 1)[1].split() for line in
                               release.read_text(encoding="utf-8", errors="replace").splitlines()
                               if line.startswith("Components:")), [])
            if components:
                entries.append((suite, suite.name, components))
        if not entries:
            raise FileNotFoundError("Terminal server APT repository directory has no APT index (Packages or Release "
                                    f"at the root, or a suite below dists/ whose Release names its Components): {repository}")
        lines = []
        for directory, suite, components in entries:
            needed = [component for component in components or [""] if source is None or not source.covers(suite, component)]
            if not needed:
                continue
            signed = (directory / "InRelease").is_file() or (directory / "Release.gpg").is_file()
            options = "" if signed else "[trusted=yes] "
            lines.append(f"deb {options}file:{APT_REPOSITORY} {suite} {' '.join(needed)}".rstrip() + "\n")
        generated = "".join(lines)
        if generated and source is not None:
            apt.check_combination(source, generated)
        return generated or "# apt_source_file names this repository.\n"

    def terminal_apt_repository_dir(self) -> Path | None:
        configured = self.settings.terminal_server_apt_repository_dir
        if configured is None:
            return None
        directory = (self.project_dir / configured.expanduser()).resolve()
        if not directory.is_dir():
            raise FileNotFoundError(f"Terminal server APT repository directory is missing: {directory}")
        if not any((directory / name).exists() for name in ("dists", *FLAT_APT_INDEXES)):
            raise FileNotFoundError(
                f"Terminal server APT repository directory has no APT index (Packages, Release or dists/): {directory}")
        return directory

    def environment(self, variables: dict[str, str]) -> dict[str, str]:
        return {**os.environ, **variables}

    def trace(self, step: Step, variables: dict[str, str] | None = None) -> None:
        defaults = config.Settings().compose_variables()
        assignments = []
        values = self.variables(step) if variables is None else variables
        if "LOAF_TERMINAL_BUILD_APT_SOURCE" in values:
            print(f"Terminal APT source file {self.settings.terminal_server_apt_source_file}", flush=True)
        for name, value in values.items():
            if name in APT_BUILD_SECRETS:
                continue
            if value == defaults.get(name) and name not in os.environ:
                continue
            if sys.platform == "win32":
                escaped = value.replace("'", "''")
                assignments.append(f"$env:{name} = '{escaped}';")
            else:
                assignments.append(f"{name}={shlex.quote(value)}")
        print("+ " + " ".join([*assignments, display_command(self.command(step))]), flush=True)

    def preview(self, step: Step) -> None:
        if self.verbose:
            self.trace(step)
        else:
            print(f"  {step.description}")

    def execute(self, step: Step) -> None:
        action = step.arguments[0]
        if self.uses_terminal_runtime(step):
            self.stage_terminal_runtime()
        variables = self.variables(step)
        if self.verbose:
            self.trace(step, variables)
        elif step.description and action not in ("config", "ps"):
            print(f"{step.description}…", file=sys.stderr, flush=True)
        env = self.environment(variables)
        wait = action == "up" and "--wait" in step.arguments
        with (wait_status("Waiting for services to become healthy") if wait else nullcontext()):
            try:
                subprocess.run(self.command(step), cwd=self.project_dir, check=True, shell=False, env=env)
            except subprocess.CalledProcessError:
                if action != "config":
                    note_docker_access(env, self.project_dir)
                raise


def execute_plan(
    compose: Compose, steps: Sequence[Step], *, lock_path: Path | None, dry_run: bool = False
) -> None:
    """Run or preview the steps. Without lock_path, the caller holds the project lock."""
    if dry_run:
        # No lock creation or Docker probing: preview works on an unprepared host.
        if steps and not compose.verbose:
            print(f"Plan for {compose.project_dir} ({compose.project_name})")
        for step in steps:
            compose.preview(step)
        return
    # Hold one lock across the whole transition. check=True stops the sequence at
    # its first failure; successful downloads/exports remain available to retry.
    with nullcontext() if lock_path is None else project_lock(lock_path):
        for step in steps:
            compose.execute(step)


class DockerQueryError(RuntimeError):
    """Docker could not report the deployment's running containers."""


DOCKER_ACCESS_HINT = "Check 'docker version' and see docs/debian.md for Docker access."


def note_docker_access(environment: dict[str, str], project: Path) -> None:
    """Check access after a failure while keeping Compose output live."""
    try:
        result = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], cwd=project,
                                env=environment, capture_output=True, check=False, timeout=5)
        if result.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(DOCKER_ACCESS_HINT, file=sys.stderr)


# compose.cpu.yml sets this in the Ollama services, which tells containers that serve on CPU apart.
CPU_ENVIRONMENT = "NVIDIA_VISIBLE_DEVICES=void"


@dataclass(frozen=True)
class ServingState:
    """The serving profiles with running containers, and whether a running Ollama service serves on CPU."""
    profiles: tuple[str, ...]
    cpu: bool


def docker_query(command: list[str], environment: dict[str, str], cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, check=False)
    if result.returncode:
        lines = result.stderr.strip().splitlines()
        raise DockerQueryError(lines[-1] if lines else f"{display_command(command[:3])} failed (exit {result.returncode}).")
    return result.stdout


def serving_state(compose: Compose) -> ServingState:
    """Ask Docker which serving profiles run and whether Ollama serves on CPU; raise DockerQueryError if it cannot."""
    step = Step(SERVING, ("ps", "--status", "running", "--format", "{{.Service}} {{.ID}}"))
    environment = compose.environment(compose.variables(step))
    try:
        running = dict(line.split(maxsplit=1) for line in
                       docker_query(compose.command(step), environment, compose.project_dir).splitlines() if line.strip())
        backends = [running[service] for profile in SERVING for service in SERVICES[profile][:1] if service in running]
        cpu = bool(backends) and any(
            CPU_ENVIRONMENT in json.loads(line) for line in docker_query(
                ["docker", "inspect", "--format", "{{json .Config.Env}}", *backends], environment).splitlines()
            if line.strip())
    except (OSError, ValueError, DockerQueryError) as exc:
        detail = str(exc).rstrip()
        detail += "" if detail.endswith((".", "?", "!")) else "."
        raise DockerQueryError(f"Docker could not report which services of {compose.project_name} run: {detail} "
                               f"{DOCKER_ACCESS_HINT}") from exc
    return ServingState(tuple(profile for profile in SERVING if any(service in running for service in SERVICES[profile])),
                        cpu)


def note_terminal_network(settings: config.Settings, mode: str, serving: str | None) -> None:
    network = settings.terminal_server_network
    if serving != "offline" or mode not in ("run", "restart") or not settings.terminal_server_enabled:
        return
    if network == "host":
        print(f"Note: [terminal_server] network = host gives chat containers access to Docker host services at {apt.HOST}.",
              flush=True)
    elif network in ("local", "internet"):
        print(f"Note: [terminal_server] network = {network}, so chat containers that models drive can reach every "
              "address the Docker host reaches.", flush=True)


def follow_logs(compose: Compose, mode: str, *, dry_run: bool, import_definitions: bool = False) -> None:
    profiles, targets = PROFILES, ()
    if mode in SERVICES:
        profiles = (mode,)
        targets = ("ollama-offline",) if import_definitions else SERVICES[mode]
    if compose.settings.terminal_server_enabled and not import_definitions and mode != "download":
        profiles += ("terminal",)
        if mode in ("online", "offline"):
            targets += ("terminal-server",)
    step = Step(profiles, ("logs", "--follow", "--tail", "0", *targets), "Follow live Compose logs")
    # Following output does not change services. Give up the project lock first
    # so another command or systemd's ExecStop can stop the deployment.
    if dry_run:
        compose.preview(step)
    else:
        compose.execute(step)


# --- Command help and argument parsing ---
@dataclass(frozen=True)
class CommandHelp:
    summary: str
    options: tuple[str, ...] = ()
    notes: str = ""


GLOBAL_OPTIONS = ("--help", "--project-dir", "--system", "--verbose")
DEPLOYMENT_OPTIONS = ("--config-dir", "--namespace", "--dry-run")
COMPOSE_OPTIONS = (*DEPLOYMENT_OPTIONS, "--attach")
START_OPTIONS = (*COMPOSE_OPTIONS, "--wait-timeout")
TRANSFER_OPTIONS = (*DEPLOYMENT_OPTIONS, "--backup-dir", "--archive")
SETUP_OPTIONS = ("--config-dir", "--dry-run", "--archive", "--force", "--allow-root")
# Setup parts in the order they run. all selects the default parts of the installation; the others run when named.
SETUP_PARTS = ("docker-deps", "venv", "toolkit", "config", "docs", "terminal-server-key", "create-file-repository")
CHECKOUT_PARTS = ("venv", "toolkit", "config")
STANDALONE_PARTS = ("config", "docs")
SYSTEM_PARTS = ("terminal-server-key",)
# loaf uses the deployment files of its installation; the docker-project link of earlier releases is gone.
NO_DOCKER_PROJECT = ("loaf uses the deployment files of its own installation, so there is no docker-project link "
                     "anymore. List the containers with 'loaf docker ps' and use 'docker logs' or 'docker exec' "
                     "with their names.")
# Options that became setup parts or another option, with their new form.
RENAMED_OPTIONS = {
    "--with-docker-deps": "loaf setup docker-deps", "--terminal-server-key": "loaf setup terminal-server-key",
    "--create-file-repository": "loaf setup create-file-repository",
    "--yes": "--force",
}
ADOPT_NOTE = "Run inside a copied backup folder to make it the project. The backup moves to backup/.\n"
SETUP_NOTES = (
    "Parts are all, docker-deps, venv, toolkit, config, docs, terminal-server-key,\n"
    "create-file-repository and tests. all is the default: venv, toolkit and config\n"
    "in a source checkout, config and docs elsewhere. Name the others to run them.\n"
    "An existing file stays; --force replaces it. 'setup tests' caches and installs\n"
    "the test packages and takes no other part.\n" + ADOPT_NOTE.rstrip("\n")
)
COMMAND_HELP = {
    "setup": CommandHelp("Prepare a deployment or install loaf from source.", SETUP_OPTIONS, SETUP_NOTES),
    "install": CommandHelp("Alias for setup.", SETUP_OPTIONS, SETUP_NOTES),
    "package": CommandHelp(
        "Build the standalone executable or Debian package into dist/.", ("--dry-run",),
        "Targets are binary (dist/loaf) and debian (dist/loaf_VERSION_all.deb).\n"
        "Run from the loaf source directory or select it with --project-dir.",
    ),
    "clean": CommandHelp(
        "Remove generated temporary files and the runtime cache.", ("--dry-run", "--full", "--force"),
        "--full asks before it removes install-cache/, whose installers an offline host cannot download again.",
    ),
    "version": CommandHelp("Show the loaf release, installation and default deployment."),
    "docs": CommandHelp(
        "List the bundled guides or show one.", (),
        "Run 'loaf docs NAME' to read a guide. 'loaf docs loaf.1' prints the manual page:\n"
        "loaf docs loaf.1 | man -l -",
    ),
    "completion": CommandHelp("Install, remove or print Bash completion for loaf."),
    "build": CommandHelp("Build the local deployment images.", (*COMPOSE_OPTIONS, "--with-apt-repos")),
    "upgrade": CommandHelp("Pull updated base images and rebuild local images.", (*COMPOSE_OPTIONS, "--with-apt-repos")),
    "download": CommandHelp("Pull the selected Ollama models.", (*START_OPTIONS, "--models-file")),
    "run": CommandHelp(
        "Start Ollama and WebUI: offline for disconnected use, online with internet access.",
        (*START_OPTIONS, "--cpu"), "Profiles are offline and online. Starting one stops the other.",
    ),
    "restart": CommandHelp(
        "Stop a serving profile and start it again with new containers.", (*START_OPTIONS, "--cpu"),
        "Profiles are offline and online. Without one, restart restarts the profile that serves now.",
    ),
    "status": CommandHelp("Check deployment readiness and list containers.", (*COMPOSE_OPTIONS, "--cpu")),
    "stop": CommandHelp(
        "Stop deployment services and remove their containers.", (*COMPOSE_OPTIONS, "--no-remove-containers"),
        "Name offline or online to stop only that profile. Without a profile, stop stops every service.",
    ),
    "docker": CommandHelp("List or remove the deployment's containers, images, volumes and networks."),
    "backup": CommandHelp(
        "Save the deployment in a portable backup folder.",
        (*TRANSFER_OPTIONS, "--verify", "--no-terminal-server", "--export", "--models-file", "--wait-timeout",
         "--attach"),
        "Types are source, config, installers, images, models, webui and terminal.\n"
        "project means config and installers; volumes means models, webui and terminal.\n"
        "terminal adds webui. Omit types to save all available ones; terminal follows\n"
        "[terminal_server] enabled and include_in_backup. Use --archive with --verify.",
    ),
    "restore": CommandHelp(
        "Restore selected types from a backup.",
        (*TRANSFER_OPTIONS, "--delete", "--import", "--cpu", "--wait-timeout", "--attach"),
        "Types are those of backup. Omit them to restore everything in the archive.\n"
        "models merge into the model store; add --delete to mirror them. webui and\n"
        "terminal need new or empty volumes. source replaces the archived files in the project.\n"
        + ADOPT_NOTE.rstrip("\n"),
    ),
    "integration": CommandHelp("Print a host integration."),
    "test": CommandHelp(
        "Run the checkout's verification suite.", ("--config-dir", "--cpu", "--badges"),
        "Use --cpu to run the Docker and model workflow tests with CPU inference.",
    ),
}
INTEGRATION_HELP = {
    "systemd": CommandHelp("Print a systemd unit for offline serving.", ("--config-dir", "--namespace", "--cpu")),
    "continue": CommandHelp("Print Continue YAML for the selected models.", ("--config-dir", "--models-file")),
    "tether": CommandHelp("Print Open WebUI Tether installation instructions."),
}
DOCKER_HELP = {
    "ps": CommandHelp("List the deployment's containers, images, volumes and networks.", ("--config-dir", "--namespace")),
    "rm": CommandHelp(
        "Remove the deployment's Docker resources of the selected types.",
        ("--config-dir", "--namespace", "--dry-run", "--force"),
        "Types are all, containers, images, volumes and networks. rm lists the resources\n"
        "and asks before it removes them. Volumes hold the models, the Open WebUI accounts\n"
        "and chats and the terminal user folders: save them first with loaf backup.\n"
        "Networks, volumes and images can go once the deployment has no containers.",
    ),
}
COMPLETION_HELP = {
    "install": CommandHelp(
        "Install Bash completion for the current user.", (),
        "bash-completion loads it in new shells. The Debian package installs it system-wide instead.",
    ),
    "remove": CommandHelp("Remove the Bash completion that install created."),
    "print": CommandHelp("Print the Bash completion script."),
}
SUBCOMMAND_HELP = {"integration": INTEGRATION_HELP, "docker": DOCKER_HELP, "completion": COMPLETION_HELP}
CLI_USAGE = "%(prog)s [GLOBAL OPTIONS] COMMAND [OPTIONS]"


def wait_timeout_option(value: str) -> int:
    try:
        return config.wait_timeout_value(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r}: expected 0 to wait without a limit or a positive number "
                                         "of seconds") from exc


def add_options(parser, selected: tuple[str, ...] | None = None) -> None:
    def add(*flags, **settings):
        if selected is None or flags[-1] in selected:
            parser.add_argument(*flags, **settings)

    add("-h", "--help", action="store_true", help="Show help and exit.")
    add("--project-dir", type=Path, metavar="PATH",
        help="Use PATH as the project (default: current directory).")
    add("--system", action="store_true", help="Use the system deployment: settings in /etc/loaf, data in /var/lib/loaf.")
    add("-v", "--verbose", action="store_true", help="Show external commands or individual test names.")
    add("--config-dir", type=Path, metavar="PATH", help="Use PATH for settings (default: project config/).")
    add("--namespace", dest="namespace_name", metavar="NAME",
        help="Select the deployment namespace loaf-NAME. Use --namespace= for the default namespace loaf.")
    add("--dry-run", action="store_true", help="Preview without running Docker or changing the project.")
    add("--models-file", type=Path, metavar="PATH", help="Select the model INI for this run.")
    add("--backup-dir", type=Path, metavar="PATH", help="Override the configured archive directory.")
    add("--archive", type=Path, metavar="PATH", help="Select a backup folder or archive file.")
    add("--verify", action="store_true", help="Check an existing backup's hashes and image platforms without Docker.")
    add("--no-terminal-server", action="store_true",
        help="Leave out the Open Terminal image, the terminal user volume and the bearer key; restore then disables "
             "the terminal server.")
    add("--delete", action="store_true", help="Mirror the archived models: delete destination model data absent from it.")
    add("--export", dest="export_models", action=argparse.BooleanOptionalAction,
        help="Enable or disable exporting selected Modelfiles from the cached models before backup.")
    add("--import", dest="import_models", action=argparse.BooleanOptionalAction,
        help="Enable or disable starting offline Ollama to apply saved Modelfiles after restore.")
    add("--no-remove-containers", dest="remove_containers", action="store_false", default=None,
        help="Keep the stopped service containers and their logs.")
    add("--cpu", action="store_true", help="Use CPU inference instead of the default NVIDIA GPU.")
    add("--allow-root", action="store_true",
        help="Prepare a source checkout as root although another user owns it.")
    add("--badges", action="store_true", help="Record test results and update badges for the groups that ran.")
    add("--full", action="store_true", help="Also remove generated installation files.")
    add("-f", "--force", action="store_true",
        help="Replace what the named setup parts would keep, or remove without asking for confirmation.")
    add("--wait-timeout", type=wait_timeout_option, metavar="SECONDS",
        help="Override the service readiness wait from config.ini. 0 waits without a limit.")
    add("-a", "--attach", action="store_true", help="Follow live Compose logs after the workflow.")
    add("--with-apt-repos", action="store_true",
        help="Build the terminal image with [terminal_server] apt_source_file and apt_repository_dir.")
    if selected is None:
        # Former options only point to their new form.
        for flag in (*RENAMED_OPTIONS, "--link-docker-project"):
            parser.add_argument(*(("-y",) if flag == "--yes" else ()), flag, dest=f"renamed_{flag[2:].replace('-', '_')}",
                                action="store_true", help=argparse.SUPPRESS)


def help_listing(heading: str, commands: dict[str, CommandHelp]) -> str:
    width = max(map(len, commands))
    rows = (f"  {name:<{width}}  {details.summary}" for name, details in commands.items())
    return "\n".join((heading, *rows))


def command_arguments(command: str, subcommand: str | None = None) -> str:
    """The arguments that a command or a subcommand takes after its name, for its usage line."""
    if command in TRANSFER_MODES:
        return "[OPTIONS] [TYPE ...]"
    if command in INSTALL_MODES:
        return "[OPTIONS] [PART ...]"
    if command == "package":
        return "[OPTIONS] TARGET ..."
    if command == "run":
        return "[OPTIONS] PROFILE"
    if command in ("stop", "restart"):
        return "[OPTIONS] [PROFILE]"
    if command == "docs":
        return "[NAME]"
    if command == "integration" and subcommand is None:
        return "INTEGRATION [OPTIONS]"
    if command in SUBCOMMAND_HELP and subcommand is None:
        return "COMMAND [OPTIONS]"
    if subcommand == "rm":
        return "[OPTIONS] TYPE ..."
    return "[OPTIONS]"


def show_help(command: str | None, subcommand: str | None) -> None:
    prog = " ".join(part for part in ("loaf", command, subcommand) if part)
    usage = CLI_USAGE if command is None else f"%(prog)s {command_arguments(command, subcommand)}"
    options = ()
    if command is None:
        description = f"{__doc__}\n\n{help_listing('Commands', COMMAND_HELP)}"
        notes = "Run 'loaf COMMAND --help' for command options.\nSee docs/cli-help.md for the full reference."
    elif command == "integration" and subcommand is None:
        description = f"{COMMAND_HELP[command].summary}\n\n{help_listing('Integrations', INTEGRATION_HELP)}"
        notes = "Run 'loaf integration INTEGRATION --help' for integration options."
    elif command in SUBCOMMAND_HELP and subcommand is None:
        description = f"{COMMAND_HELP[command].summary}\n\n{help_listing('Commands', SUBCOMMAND_HELP[command])}"
        notes = f"Run 'loaf {command} COMMAND --help' for command options."
    else:
        details = COMMAND_HELP[command] if subcommand is None else SUBCOMMAND_HELP[command][subcommand]
        description, options, notes = details.summary, details.options, details.notes
    parser = argparse.ArgumentParser(
        prog=prog, usage=usage, description=description, epilog=notes, add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    if options:
        add_options(parser.add_argument_group("Options"), options)
    add_options(parser.add_argument_group("Global options"), GLOBAL_OPTIONS)
    parser.print_help()
    parser.exit()


def project_command(project: Path, config_dir: Path | None, namespace_name: str | None, *,
                    system: bool = False) -> list[str]:
    executable = os.environ.get("LOAF_RELEASE_EXECUTABLE")
    if executable and setup.bundled_release() is not None and transfer.standalone_executable(Path(executable)):
        command = [executable]
    else:
        # Isolated mode keeps a writable working directory out of the module path.
        command = [sys.executable, *(("-I",) if system else ()), "-m", "loaf"]
    command.extend(("--system",) if system else ("--project-dir", str(project)))
    if config_dir is not None:
        command.extend(("--config-dir", str(config_dir)))
    if namespace_name:
        command.extend(("--namespace", namespace_name))
    elif namespace_name is not None:
        command.append("--namespace=")
    return command


# --- Follow-up hints for the backup workflow ---
def active_virtualenv() -> bool:
    """Whether this loaf runs from the activated virtualenv."""
    environment = os.environ.get("VIRTUAL_ENV")
    return bool(environment) and Path(environment).resolve() == Path(sys.prefix).resolve()


def own_launcher(project: Path) -> bool:
    """Whether the project is the current directory and holds its own launcher: the standalone executable, or the
    ./loaf link to the virtualenv that source setup creates."""
    launcher = project / transfer.LAUNCHER
    checkout = launcher.is_symlink() and launcher.readlink() == setup.CHECKOUT_LAUNCHER and launcher.is_file()
    return project == Path.cwd().resolve() and (checkout or transfer.standalone_executable(launcher))


def next_command(project: Path, config_dir: Path | None, namespace_name: str | None, *arguments: str,
                 system: bool = False) -> str:
    """A copyable follow-up command; inside a project with its own launcher, the short ./loaf form."""
    command = project_command(project, config_dir, namespace_name, system=system)
    if not system and own_launcher(project):
        command[:command.index("--project-dir") + 2] = ["./loaf"]
    elif command[:3] == [sys.executable, "-m", "loaf"] and active_virtualenv():
        # The activated virtualenv puts its loaf on PATH.
        command[:3] = ["loaf"]
    return display_command([*command, *arguments])


def docs_hint(project: Path) -> str:
    # Without options, the command ends in --project-dir PATH, which docs does not take.
    loaf = ["./loaf"] if own_launcher(project) else project_command(project, None, None)[:-2]
    return f"Read the guides with {display_command([*loaf, 'docs'])}, for example {display_command([*loaf, 'docs', 'transfer'])}."


def system_restore_hint(backup: Path) -> str:
    command = project_command(assets.SYSTEM_PROJECT_DIR, None, None, system=True)
    return ("The loaf Debian package is installed. To restore into its system deployment instead, run:\n"
            f"  sudo {display_command([*command, 'restore', '--archive', str(backup)])}")


def backup_folder_hints(folder: Path) -> str:
    """The typical next steps for a directory that holds a backup and no project."""
    loaf = display_command([str(folder / transfer.LAUNCHER)])
    lines = [
        f"{folder} is a backup folder. The typical next steps in it are:",
        "  ./loaf setup        make the folder the project; the backup moves to backup/",
        "  ./loaf restore      restore everything in the backup",
        "  ./loaf run offline  start Ollama and Open WebUI offline",
        "To keep the backup unchanged, run its loaf from a new project directory instead:",
        f"  mkdir -p ~/loaf && cd ~/loaf && {loaf} setup && {loaf} restore",
    ]
    if assets.system_deployment_installed():
        lines.append(system_restore_hint(folder))
    lines.append("Read the guides with ./loaf docs, for example ./loaf docs transfer.")
    return "\n".join(lines)


def clean_confirmation(force: bool):
    """Ask before clean --full removes files an offline host cannot download again."""
    def confirm(path: Path) -> bool:
        if force:
            return True
        if not sys.stdin.isatty():
            print(f"Keep {path}: an offline host cannot download its installers again. Add --force to remove it.")
            return False
        try:
            answer = input(f"Remove {path}? An offline host cannot download its installers again. [y/N] ")
        except EOFError:
            answer = ""
        return answer.strip().lower() in ("y", "yes")
    return confirm


def package_script(project: Path, target: str) -> Path | None:
    """Return the source checkout's build script for a package target, or None outside a checkout."""
    script = project / PACKAGE_SCRIPTS[target]
    package = project / setup.PYTHON_PACKAGE_DIR
    return script if script.is_file() and (package / "pyproject.toml").is_file() else None


def version_report() -> str:
    if assets.debian_package():
        deployment = f"system deployment ({config.system_directory()}, {assets.SYSTEM_PROJECT_DIR})"
    else:
        deployment = "current directory"
    return (
        f"loaf {assets.release_version()}\n"
        f"Installation: {assets.installation()}\n"
        f"Python: {sys.version.split()[0]} ({sys.executable})\n"
        f"Deployment data: {assets.DATA_DIR}\n"
        f"Default deployment: {deployment}\n"
    )


def report_settings(layers: Sequence[Path], namespace_name: str, config_dir: Path, env_file: Path | None) -> None:
    """List the settings files in effect; env_file is the project's .env when docker_env_enable lets Compose read it."""
    files = [str(directory / config.CONFIG_NAME) for directory in layers[1:] if (directory / config.CONFIG_NAME).is_file()]
    if env_file is not None and env_file.is_file():
        files.append(str(env_file))
    print("Settings    " + ", ".join(["packaged defaults", *files]), flush=True)
    warn_shared_deployment(layers, namespace_name, config_dir)


def warn_shared_deployment(layers: Sequence[Path], namespace_name: str, config_dir: Path) -> None:
    shared = config.shared_system_deployment(tuple(layers), namespace_name)
    if shared is not None:
        print(
            f"Warning: this project and the system deployment in {shared} use the same Docker namespace "
            f"{config.namespace(namespace_name)}. Set [launcher] namespace in "
            f"{config_dir / config.CONFIG_NAME} for a separate deployment, or set system_config = to skip "
            "the system settings.", file=sys.stderr, flush=True,
        )


def run_build_script(project: Path, command: list[str], *, verbose: bool) -> int:
    if verbose:
        print("+ " + display_command(command), flush=True)
    env = {**os.environ, "LOAF_BUILD_PYTHON": sys.executable}
    return subprocess.run(command, cwd=project, env=env, check=False).returncode


def build_executable(project: Path, script: Path, *, verbose: bool) -> int:
    return run_build_script(project, ["bash", str(script), "--binary-only"], verbose=verbose)


def build_debian_package(project: Path, script: Path, *, verbose: bool) -> int:
    """Build the Debian package with the standalone executable, which backups of the system deployment carry."""
    if shutil.which("dpkg-deb") is None:
        raise setup.SetupError("package debian requires dpkg-deb. Run it on Debian or Ubuntu.")
    return run_build_script(project, ["bash", str(script), "--debian-only", str(project / DIST_DIR)], verbose=verbose)


PACKAGE_BUILDERS = {"binary": build_executable, "debian": build_debian_package}


def docker_resources(command: str, types: Sequence[str], namespace: str, lock_path: Path, *, dry_run: bool, force: bool,
                     verbose: bool, with_containers: str) -> int:
    """List the deployment's Docker resources, or remove the selected types after confirmation."""
    if command == "ps":
        print(resources.report(namespace, resources.inventory(namespace)))
        return 0
    if dry_run:
        print(f"Plan for deployment {namespace}")
        for step in resources.plan(namespace, types):
            print(f"  {step}")
        return 0
    # The lock keeps loaf from starting services while they are removed.
    with project_lock(lock_path):
        if "containers" not in types and (remaining := resources.containers(namespace)):
            print(f"Error: Deployment {namespace} still has {resources.quantity(len(remaining), 'container')}, "
                  f"which can use its networks, volumes and images. Remove the containers as well:\n"
                  f"  {with_containers}", file=sys.stderr)
            return 1
        found = resources.inventory(namespace, types)
        print(resources.report(namespace, found), flush=True)
        total = sum(map(len, found.values()))
        if not total:
            print("\nNothing to remove.")
            return 0
        if not force:
            if not sys.stdin.isatty():
                print("Error: Confirm the removal in a terminal, or add --force.", file=sys.stderr)
                return 1
            try:
                answer = input(f"\nRemove {resources.quantity(total, 'resource')}? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing removed.")
                return 1
        removed = resources.remove(namespace, types, verbose=verbose)
    print(f"Removed {resources.quantity(removed, 'Docker resource')} of deployment {namespace}.")
    return 0


def command_failure(exc: subprocess.CalledProcessError) -> int:
    program = exc.cmd if isinstance(exc.cmd, str) else exc.cmd[0]
    print(f"{program} failed (exit {exc.returncode}).", file=sys.stderr)
    # Negative signal statuses and shell-reserved exit values become a stable
    # generic failure; ordinary Docker failures retain their useful exit code.
    return exc.returncode if 0 < exc.returncode < 126 else 1


def project_ready(project: Path, config_dir: Path) -> bool:
    """Whether the directory is a project: a source checkout, or a deployment with settings that setup created."""
    return (project / setup.PYTHON_PACKAGE_DIR / "pyproject.toml").is_file() or (config_dir / config.CONFIG_NAME).is_file()


# --- Setup parts ---
def setup_command(args, parts: Sequence[str], *, project: Path, config_dir: Path, override_dir: Path | None,
                  layer_dirs: Sequence[Path], settings: config.Settings, namespace: str, namespace_name: str,
                  archive: transfer.Archive | None, adopted: Path | None, system: bool, standalone: bool) -> int:
    """Run the selected setup parts in their order. What a part would replace stays unless --force is given."""
    dry_run, force = args.dry_run, args.force
    lock_path = project / PROJECT_LOCK
    key_path = config.terminal_secret_path(project, settings.terminal_server_secret_file)
    models_name = None if settings.models_file is not None else "models.ini"
    host_parts = [part for part in setup.HOST_PARTS if part in parts]
    if system and not parts:
        print(f"The Debian package prepared the system deployment: settings in {config_dir}, data in {project}.")
    if dry_run:
        source = f" from {archive.path}" if archive is not None and standalone else ""
        descriptions = {
            "docker-deps": f"Cache and, if needed, install Debian Docker Engine, Compose and Buildx packages{source}.",
            "venv": f"Prepare {project / '.venv'} and cache the Python wheels.",
            "toolkit": f"Cache and, if needed, install the NVIDIA Container Toolkit packages{source}.",
            "config": f"Create {config_dir / config.CONFIG_NAME} and the model list in effect"
                      + (f", replacing existing files; a replaced file stays as NAME{setup.PREVIOUS_SUFFIX}."
                         if force else " if missing."),
            "docs": f"Write the guides for loaf {assets.release_version()} to {project / docs.EXPORT_DIR}"
                    + (", replacing an existing directory." if force else " if missing."),
        }
        for part in parts:
            if part in descriptions:
                print(descriptions[part])
            elif part == "terminal-server-key":
                from_backup = archive is not None and "terminal" in archive.items
                print(f"{'Replace' if force and key_path.exists() else 'Create'} the terminal server key at {key_path}"
                      + (" if missing" if not force else "") + (f", from {archive.path}." if from_backup else "."))
            elif part == "create-file-repository":
                image = transfer.image_names(namespace, include_terminal=True)[-1]
                print(f"Download {' '.join(setup.TEST_REPOSITORY_PACKAGES)} and the dependencies that {image} lacks "
                      f"into {project / setup.TEST_REPOSITORY_DIR} and write {project / setup.TEST_REPOSITORY_SOURCE}.")
        return 0
    with project_lock(lock_path):
        try:
            if host_parts and standalone:
                if archive is not None:
                    transfer.setup_from_archive(archive, project, installers=True)
                if "docker-deps" in host_parts:
                    setup.prepare_docker(project, allow_downloads=archive is None)
                if "toolkit" in host_parts:
                    setup.install_toolkit(project, allow_downloads=archive is None)
            elif host_parts:
                setup.install(project, host_parts, allow_root=args.allow_root)
        except (OSError, setup.SetupError, subprocess.CalledProcessError):
            print("See docs/debian.md for Debian host setup.", file=sys.stderr)
            raise
        if "config" in parts:
            setup.prepare_configuration(config_dir, layer_dirs[:-1], models_name, force=force)
        if "docs" in parts:
            docs.export(project, force=force)
        if "terminal-server-key" in parts:
            # In a backup folder, the backup's key: its Open WebUI data uses it.
            from_backup = archive is not None and "terminal" in archive.items and (force or not key_path.exists())
            archived_key = transfer.setup_from_archive(archive, project, installers=False) if from_backup else None
            setup.ensure_terminal_key(key_path, archived_key, group=setup.SYSTEM_GROUP if system else None, force=force)
        if "create-file-repository" in parts:
            setup.create_test_repository(project, transfer.image_names(namespace, include_terminal=True)[-1],
                                         force=force)
        launcher = project / transfer.LAUNCHER
        if standalone and archive is not None and archive.executable is not None and not (
                launcher.exists() or launcher.is_symlink()):
            # Keep a launcher in the project; the backup folder can stay on its media.
            shutil.copyfile(archive.executable, launcher)
            launcher.chmod(transfer.file_modes(project)[1])
    if standalone and "config" in parts:
        print(f"Deployment ready in {project}.")
    if not system:
        warn_shared_deployment(layer_dirs, namespace_name, config_dir)
    if "venv" in parts:
        activate = Path(".venv/bin/activate") if project == Path.cwd().resolve() else project / ".venv/bin/activate"
        print(f"Activate the virtualenv to run loaf: source {shlex.quote(str(activate))}")
    if standalone and archive is not None:
        backup = archive.path.parent if archive.executable is not None else archive.path
        # The project finds only the newest backup of its configured namespace.
        found = transfer.latest_backup(project / settings.backup_dir, namespace=config.namespace(settings.namespace_name))
        selection = () if found == backup else ("--archive", str(backup))
        print("Next: " + next_command(project, override_dir, None, "restore", *selection))
        print(docs_hint(project))
        if adopted is not None and assets.system_deployment_installed():
            print(system_restore_hint(adopted))
    elif settings.terminal_server_enabled and not key_path.exists():
        print("The terminal server is enabled. Create its key with: "
              + next_command(project, override_dir, None, "setup", "terminal-server-key", system=system))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # Only full option names: a prefix such as --all must not select --allow-root.
    parser = argparse.ArgumentParser(prog="loaf", usage=CLI_USAGE, add_help=False, allow_abbrev=False)
    parser.add_argument("mode", nargs="?")
    parser.add_argument("arguments", nargs="*")
    add_options(parser)
    args = parser.parse_intermixed_args(argv)
    for flag, replacement in RENAMED_OPTIONS.items():
        if getattr(args, f"renamed_{flag[2:].replace('-', '_')}"):
            parser.error(f"{flag} is now {replacement}.")
    if args.renamed_link_docker_project:
        parser.error(NO_DOCKER_PROJECT)
    if args.mode is not None and args.mode not in COMMAND_HELP:
        if args.mode in SERVING:
            parser.error(f"loaf {args.mode} is now loaf run {args.mode}.")
        parser.error(f"argument mode: invalid choice: {args.mode!r} (choose from {', '.join(COMMAND_HELP)})")
    serving = None
    integration = None
    docker_command = None
    docker_types = ()
    completion_command = None
    targets = ()
    components = tuple(args.arguments) if args.mode in TRANSFER_MODES and args.arguments else None
    setup_tests = args.mode in INSTALL_MODES and args.arguments == ["tests"]
    parts = ()
    if args.mode in INSTALL_MODES and not setup_tests:
        parts = tuple(args.arguments) or ("all",)
        if "tests" in parts:
            parser.error("setup tests installs only the test packages. Name it alone.")
        if "link-docker-project" in parts:
            parser.error(NO_DOCKER_PROJECT)
        if not set(parts) <= {"all", *SETUP_PARTS}:
            parser.error(f"invalid setup part. Use all, {', '.join(SETUP_PARTS)} or tests.")
        if len(set(parts)) != len(parts):
            parser.error("Specify each setup part once.")
    docs_name = args.arguments[0] if args.mode == "docs" and len(args.arguments) == 1 else None
    if args.mode == "integration" and args.arguments:
        if len(args.arguments) != 1 or args.arguments[0] not in INTEGRATION_HELP:
            parser.error("invalid choice for integration. Use systemd, continue or tether.")
        integration = args.arguments[0]
    elif args.mode == "docker" and args.arguments:
        docker_command, *docker_types = args.arguments
        if docker_command not in DOCKER_HELP:
            parser.error("invalid docker command. Use ps or rm.")
        if docker_command == "ps" and docker_types:
            parser.error("docker ps takes no arguments. See 'loaf docker ps --help'.")
        if not set(docker_types) <= set(resources.SELECTABLE):
            parser.error("invalid resource type. Use all, containers, images, volumes or networks.")
        if len(set(docker_types)) != len(docker_types):
            parser.error("Specify each resource type once.")
        docker_types = tuple(kind for kind in resources.KINDS if "all" in docker_types or kind in docker_types)
    elif args.mode == "completion" and args.arguments:
        if len(args.arguments) != 1 or args.arguments[0] not in COMPLETION_HELP:
            parser.error("invalid completion command. Use install, remove or print.")
        completion_command = args.arguments[0]
    elif args.mode in ("run", "stop", "restart") and args.arguments:
        if len(args.arguments) != 1 or args.arguments[0] not in SERVING:
            parser.error(f"{args.mode} takes one profile: offline or online.")
        serving = args.arguments[0]
    elif args.mode == "package" and args.arguments:
        targets = tuple(args.arguments)
        if not set(targets) <= set(PACKAGE_SCRIPTS):
            parser.error("invalid package target. Use binary, debian or both.")
        if len(set(targets)) != len(targets):
            parser.error("Specify each package target once.")
    elif components is None and args.arguments and args.mode not in INSTALL_MODES and docs_name is None:
        if args.arguments[0] in INTEGRATION_HELP:
            parser.error("Use the integration name with loaf integration.")
        parser.error(f"Unexpected arguments for {args.mode}. See 'loaf {args.mode} --help'.")
    if args.help:
        show_help(args.mode, integration or docker_command or completion_command)
    if args.mode is None:
        folder = (args.project_dir or Path.cwd()).resolve()
        if not args.system and transfer.is_bundle(folder):
            print(f"loaf: error: A command is required.\n{backup_folder_hints(folder)}", file=sys.stderr)
            return 2
        parser.error("A command is required. See 'loaf --help'.")
    if args.mode == "integration" and integration is None:
        parser.error("integration requires a name: systemd, continue or tether.")
    if args.mode == "run" and serving is None:
        parser.error("run requires a profile: offline or online.")
    if args.mode == "restart" and serving is None and args.dry_run:
        parser.error("A dry run cannot ask Docker which profile serves. Name offline or online.")
    if args.mode == "package" and not targets:
        parser.error("package requires a target: binary, debian or both.")
    if args.mode == "docker" and docker_command is None:
        parser.error("docker requires a command: ps or rm.")
    if args.mode == "completion" and completion_command is None:
        parser.error("completion requires a command: install, remove or print.")
    if docker_command == "rm" and not docker_types:
        parser.error("docker rm requires one or more types: all, containers, images, volumes or networks.")
    if docker_command == "ps" and args.dry_run:
        parser.error("docker ps changes nothing. Omit --dry-run.")
    if args.force and docker_command != "rm" and not (args.mode == "clean" and args.full) \
            and args.mode not in INSTALL_MODES:
        parser.error("--force is only supported by setup, docker rm and clean --full.")
    continue_integration = integration == "continue"
    named = ()
    if components:
        if len(set(components)) != len(components):
            parser.error("Specify each archive type once.")
        try:
            named = transfer.expand_types(components)
        except transfer.TransferError as exc:
            parser.error(str(exc))
    # Named types decide these options before any backup is searched; the defaults are checked later.
    if args.mode == "backup" and args.no_terminal_server and "terminal" in (components or ()):
        parser.error("--no-terminal-server leaves out the terminal type. Omit one of them.")
    if args.mode == "backup" and args.export_models and named and "models" not in named:
        parser.error("--export requires the models type.")
    if args.mode == "restore" and named and "models" not in named:
        if args.delete:
            parser.error("--delete applies to the models type of restore.")
        if args.import_models:
            parser.error("--import requires the models type.")
    if args.remove_containers is not None and args.mode != "stop":
        parser.error("--no-remove-containers is only supported by stop.")
    remove_containers = args.remove_containers is not False
    if args.verify and args.mode != "backup":
        parser.error("--verify is only supported by backup.")
    if args.verify and components:
        parser.error("--verify checks the whole archive. Omit the types.")
    if args.no_terminal_server and (args.mode != "backup" or args.verify):
        parser.error("--no-terminal-server is only supported when creating a backup.")
    if args.allow_root and args.mode not in INSTALL_MODES:
        parser.error("--allow-root is only supported by setup and install.")
    if args.delete and args.mode != "restore":
        parser.error("--delete is only supported by restore, for the models type.")
    if args.cpu and args.mode not in ("run", "restart", "status", "restore", "test") and integration != "systemd":
        parser.error("--cpu is only supported by run, restart, status, restore, test and integration systemd.")
    if setup_tests and (args.archive is not None or args.force):
        parser.error("setup tests installs only the test packages. Omit --archive and --force.")
    if args.with_apt_repos and args.mode not in ("build", "upgrade"):
        parser.error("--with-apt-repos is only supported by build and upgrade.")
    if args.badges and args.mode != "test":
        parser.error("--badges is only supported by test.")
    if args.full and args.mode != "clean":
        parser.error("--full is only supported by clean.")
    if args.backup_dir is not None and args.mode not in TRANSFER_MODES:
        parser.error("--backup-dir is only supported by backup and restore.")
    if args.attach and (args.verify or args.mode not in (*COMPOSE_COMMANDS, *TRANSFER_MODES)):
        parser.error("--attach is available for Compose and transfer commands.")
    if args.system and (args.project_dir is not None or args.config_dir is not None):
        parser.error("--system uses /etc/loaf and /var/lib/loaf. Omit --project-dir and --config-dir.")
    unexpected = any(value is not None for value in (
        args.config_dir, args.namespace_name, args.models_file, args.backup_dir, args.archive,
        args.export_models, args.import_models, args.wait_timeout,
    ))
    if args.mode in ("version", "docs", "completion"):
        if unexpected or args.dry_run or args.project_dir is not None:
            parser.error(f"Unexpected option for {args.mode}. See 'loaf {args.mode} --help'.")
        if args.mode == "completion":
            try:
                print(completion.script() if completion_command == "print"
                      else completion.install() if completion_command == "install" else completion.remove())
            except OSError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            return 0
        if args.mode == "version":
            print(version_report(), end="")
            return 0
        try:
            docs.show(docs_name)
        except BrokenPipeError:
            # A pager or head closed the output early.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0
    # The Debian package operates the system deployment unless a project is selected.
    system_mode = args.system or (assets.debian_package() and args.project_dir is None and args.config_dir is None)
    if system_mode and args.mode in INSTALL_MODES and (
        setup_tests or not set(parts) <= {"all", *SYSTEM_PARTS} or args.archive is not None
    ):
        parser.error("The Debian package prepares the system deployment. Name only all or terminal-server-key "
                     "and omit --archive, or select a project with --project-dir.")
    if args.force and "all" in parts:
        # Replacing settings or guides needs the part named, so a plain setup never discards them.
        parser.error("--force replaces only what the parts named on the command line would keep. Name the parts "
                     "instead of all, for example: loaf setup "
                     + ("terminal-server-key" if system_mode else "config") + " --force")
    adopt_backup = False
    if system_mode:
        project_dir = assets.SYSTEM_PROJECT_DIR
        if not project_dir.is_dir():
            print(f"Error: The system deployment directory {project_dir} is missing. "
                  "Install the Debian package, or omit --system.", file=sys.stderr)
            return 1
        cwd = Path.cwd()
        if not args.system and (cwd / "config" / config.CONFIG_NAME).is_file():
            print(f"Using the system deployment in {project_dir}. Add --project-dir . to use this directory.",
                  file=sys.stderr, flush=True)
    else:
        project_dir = (args.project_dir or Path.cwd()).resolve()
        # Setup and restore make a backup folder they run in the project; other commands need a project.
        adopt_backup = transfer.is_bundle(project_dir) and not args.verify
        if adopt_backup and args.mode not in (*INSTALL_MODES, "restore"):
            print(f"loaf: error: {args.mode} needs a project.\n{backup_folder_hints(project_dir)}", file=sys.stderr)
            return 2
    if args.mode in ("package", "clean") or setup_tests:
        if unexpected:
            parser.error(f"Unexpected option for {args.mode}. See 'loaf {args.mode} --help'.")
        try:
            if setup_tests:
                if args.dry_run:
                    print(f"Cache the test package wheels in {project_dir / setup.PYTHON_WHEEL_DIR} "
                          f"and install them into {sys.prefix}.")
                    return 0
                with project_lock(project_dir / PROJECT_LOCK):
                    setup.install_test_packages(project_dir)
                return 0
            if args.mode == "clean":
                cleanup.clean(project_dir, full=args.full, dry_run=args.dry_run, confirm=clean_confirmation(args.force))
                return 0
            scripts = {target: package_script(project_dir, target) for target in targets}
            if None in scripts.values():
                parser.error("package requires the loaf source directory. Select it with --project-dir.")
            if args.dry_run:
                for target in targets:
                    print(f"Build the standalone executable at {project_dir / DIST_DIR / 'loaf'}."
                          if target == "binary" else f"Build the Debian package in {project_dir / DIST_DIR}.")
                return 0
            with project_lock(project_dir / PROJECT_LOCK):
                for target in targets:
                    result = PACKAGE_BUILDERS[target](project_dir, scripts[target], verbose=args.verbose)
                    if result:
                        return result
            return 0
        except (OSError, config.ConfigError, setup.SetupError, AlreadyRunningError, subprocess.CalledProcessError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    # Every installation uses its own deployment files; the project holds settings and data.
    compose_file = assets.DATA_DIR / "compose.yml"
    defaults_dir = assets.DATA_DIR / "config"
    if system_mode:
        # The system deployment's own settings are its top layer.
        config_dir = config.system_directory() or config.SYSTEM_CONFIG_DIR
        layer_dirs = (defaults_dir, config_dir)
    else:
        config_dir = args.config_dir.resolve() if args.config_dir else project_dir / "config"
        layer_dirs = ()
    if args.config_dir is not None and not config_dir.is_dir() and args.mode not in INSTALL_MODES:
        parser.error(f"Configuration directory does not exist: {config_dir}.")
    # Outside a source checkout, setup prepares a deployment: the standalone executable, or a wheel-installed loaf.
    source_checkout = (project_dir / setup.PYTHON_PACKAGE_DIR / "pyproject.toml").is_file()
    standalone_setup = (args.mode in INSTALL_MODES and (setup.bundled_release() is not None or not source_checkout)
                        and not system_mode)
    setup_parts = ()
    if parts:
        # A source checkout carries its own guides; only its source setup prepares its virtualenv.
        defaults = (() if system_mode else CHECKOUT_PARTS if not standalone_setup
                    else ("config",) if source_checkout else STANDALONE_PARTS)
        setup_parts = tuple(part for part in SETUP_PARTS if part in parts or ("all" in parts and part in defaults))
        if "venv" in parts and standalone_setup:
            parser.error("setup venv prepares a source checkout's virtualenv with the checkout's own setup command. "
                         "See docs/debian.md.")
        if "docs" in parts and source_checkout:
            parser.error("A source checkout carries its own guides in docs/. Omit docs.")
    if args.backup_dir is not None and args.archive is not None:
        parser.error("Use either --backup-dir or --archive.")
    if args.archive is not None and args.mode != "restore" and not (args.verify or standalone_setup):
        parser.error("--archive is for standalone setup, restore or backup --verify.")
    archive = None
    try:
        layer_dirs = layer_dirs or config.layers(defaults_dir, config_dir)
        settings = config.load(*layer_dirs)
        namespace_name = settings.namespace_name if args.namespace_name is None else args.namespace_name
        namespace = config.namespace(namespace_name)
        if args.mode == "restore" or args.verify or standalone_setup or "terminal-server-key" in setup_parts:
            # An explicit archive, else the backup folder this runs in or from, else the newest backup.
            selection = args.archive.resolve() if args.archive is not None else None
            if selection is None and args.backup_dir is None:
                selection = project_dir if adopt_backup else transfer.find_bundle(Path.cwd(), None)
            if selection is None and (args.mode == "restore" or args.verify):
                backup_dir = (args.backup_dir or project_dir / settings.backup_dir).resolve()
                selection = transfer.latest_backup(backup_dir, namespace=namespace)
                if selection is None:
                    others = sorted(path for path in backup_dir.iterdir() if transfer.is_bundle(path)) \
                        if backup_dir.is_dir() else []
                    hint = (f"It holds backups of other namespaces. Select one with --archive, for example "
                            f"--archive {display_command([str(others[-1])])}." if others
                            else "Use --backup-dir or --archive.")
                    raise transfer.TransferError(f"No backup found in {backup_dir}. {hint}")
            if selection is not None:
                archive = transfer.open_archive(selection, minimum=settings.backup_version_min,
                                                verify_launcher=not args.dry_run)
                if args.namespace_name is None or args.namespace_name == archive.namespace_name:
                    namespace_name = archive.namespace_name
                    namespace = config.namespace(namespace_name)
                elif not (args.mode == "restore" and named and set(named) <= set(transfer.PORTABLE_TYPES)):
                    raise transfer.TransferError(
                        f"The backup is of namespace {config.namespace(archive.namespace_name)}. Select the matching "
                        f"--namespace, or restore only {', '.join(transfer.PORTABLE_TYPES)} into another namespace.")
        selected = ()
        if args.mode == "backup" and not args.verify:
            terminal = settings.terminal_server_enabled and not args.no_terminal_server
            selected = (transfer.expand_types(components, terminal_available=terminal) if components else
                        transfer.default_backup_types(
                            project_dir, terminal=terminal and settings.terminal_server_include_in_backup))
        elif args.mode == "restore":
            # Restore always has an archive by now: a missing backup ended above, also in dry runs.
            selected = transfer.requested_types(archive, components)
    except (OSError, transfer.TransferError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except config.ConfigError as exc:
        parser.error(str(exc))
    except ValueError as exc:
        parser.error(f"--namespace: {exc}.")
    backup_models = args.mode == "backup" and "models" in selected
    restore_models = args.mode == "restore" and "models" in selected
    if args.delete and not restore_models:
        parser.error("--delete applies to the models type of restore.")
    if args.export_models is not None and (args.mode != "backup" or args.verify):
        parser.error("--export/--no-export are only supported when creating a backup.")
    if args.export_models and not backup_models:
        parser.error("--export requires the models type.")
    if args.import_models is not None and args.mode != "restore":
        parser.error("--import/--no-import are only supported by restore.")
    if args.import_models and not restore_models:
        parser.error("--import requires the models type.")
    export_models = backup_models and (
        settings.export_models if args.export_models is None else args.export_models
    )
    import_models = restore_models and (
        settings.import_models if args.import_models is None else args.import_models
    )
    workflow_mode = serving if args.mode in ("run", "restart") else args.mode
    if export_models:
        workflow_mode = "download"
    elif import_models:
        workflow_mode = "offline"
    if args.models_file is not None and not export_models and args.mode != "download" and not continue_integration:
        parser.error("--models-file is supported by download, backup --export and integration continue.")
    if args.wait_timeout is not None and workflow_mode not in SERVICES and args.mode != "restart":
        parser.error("--wait-timeout is only supported by commands that start services.")
    if args.with_apt_repos and not settings.terminal_server_enabled:
        parser.error("--with-apt-repos applies to the terminal image, which builds only with [terminal_server] "
                     "enabled = true.")
    if args.with_apt_repos and settings.terminal_server_apt_source_file is None \
            and settings.terminal_server_apt_repository_dir is None:
        parser.error("--with-apt-repos needs [terminal_server] apt_source_file or apt_repository_dir.")
    if args.mode == "docker":
        override = args.config_dir.resolve() if args.config_dir is not None else None
        with_containers = next_command(project_dir, override, args.namespace_name, "docker", "rm", "containers", *docker_types,
                                       system=system_mode)
        try:
            return docker_resources(docker_command, docker_types, namespace, project_dir / PROJECT_LOCK,
                                    dry_run=args.dry_run, force=args.force, verbose=args.verbose,
                                    with_containers=with_containers)
        except subprocess.CalledProcessError as exc:
            return command_failure(exc)
        except (OSError, ValueError, AlreadyRunningError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Interrupted.", file=sys.stderr)
            return 130
    if args.mode == "test":
        if args.dry_run or args.namespace_name is not None:
            parser.error("test supports --project-dir, --config-dir, -v, --cpu and --badges. See 'loaf test --help'.")
        tests_dir = project_dir / TESTS_DIR
        if not tests_dir.is_dir():
            parser.error(f"No tests directory at {tests_dir}.")
        # A child isolates suite imports. Require a nonempty suite on every
        # supported Python version.
        command = [sys.executable, "-c", TEST_RUNNER, str(tests_dir), "2" if args.verbose else "0"]
        if args.badges:
            command.append(str(Path(__file__).resolve().parents[1]))
        env = dict(os.environ)
        for flag in config.TEST_FLAGS:
            env.setdefault(flag.upper(), "1" if getattr(settings, flag) else "0")
        if args.cpu:
            env.update(RUN_DOCKER_TESTS="1", RUN_CPU_TESTS="1", RUN_GPU_TESTS="0")
        try:
            return subprocess.run(command, cwd=project_dir, check=False, env=env).returncode
        except OSError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    if integration == "tether":
        print(integrations.TETHER_INSTRUCTIONS, end="")
        return 0
    # Only the model and volume types stop and inspect Compose services.
    compose_types = {"models", *transfer.SERVICE_TYPES}
    if not (continue_integration or args.verify or (args.mode == "backup" and not compose_types & set(selected))
            or args.mode in (*INSTALL_MODES, "restore") or system_mode or project_ready(project_dir, config_dir)):
        parser.error(f"{project_dir} has no settings in {config_dir}. Run loaf setup there, or select a project "
                     "with --project-dir.")
    # restart, and stop PROFILE with the terminal server, ask Docker what runs. A real run asks and plans under the
    # project lock, so that a concurrent loaf command cannot change the answer before the steps run.
    ask_docker = not args.dry_run and (args.mode == "restart" or (
        args.mode == "stop" and serving is not None and settings.terminal_server_enabled))
    cpu_file = assets.DATA_DIR / "compose.cpu.yml"
    if (args.cpu or args.mode == "restart") and not cpu_file.is_file():
        parser.error(f"The CPU Compose override is missing: {cpu_file}.")
    # restart also serves on CPU when Ollama serves on CPU now; it decides under the lock.
    cpu = args.cpu
    compose_files = (compose_file, *((cpu_file,) if cpu else ()))
    if args.mode in INSTALL_MODES and args.namespace_name is not None:
        parser.error(f"{args.mode} prepares the checkout and host. Omit --namespace.")
    override_dir = config_dir if args.config_dir is not None else None
    lock_path = project_dir / PROJECT_LOCK
    steps = []
    try:
        if integration == "systemd":
            if sys.platform != "linux":
                parser.error("integration systemd requires Linux.")
            launcher = project_command(project_dir, override_dir, namespace_name, system=system_mode)
            print(integrations.systemd_unit(project_dir, launcher, 0 if system_mode else os.getuid(), cpu=args.cpu),
                  end="")
            return 0
        models = []
        if workflow_mode == "download" or continue_integration:
            models_file = select_models_file(layer_dirs, args.models_file, settings.models_file)
            models = read_models(models_file)
        if continue_integration:
            print(integrations.continue_config(settings.webui_url(), ((m.name, m.source) for m in models)), end="")
            return 0
        if settings.terminal_server_enabled and args.mode in ("run", "restart") and not settings.auth:
            parser.error("The terminal server requires [webui] auth = true.")
        timeout = args.wait_timeout if args.wait_timeout is not None else settings.wait_timeout
        terminal = settings.terminal_server_enabled
        if workflow_mode in MODES and (args.mode != "restore" or args.dry_run) and not ask_docker:
            steps = plan(
                "restart" if args.mode == "restart" else workflow_mode, models, wait_timeout=timeout,
                export_definitions=export_models, import_definitions=import_models,
                terminal_server=terminal, remove_containers=remove_containers, cpu=cpu,
                serving=serving if args.mode in ("stop", "restart") else None,
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.mode in (*INSTALL_MODES, "restore") and not args.dry_run and not system_mode:
        # Setup and restore create a missing project directory, but only once every check has passed.
        project_dir.mkdir(parents=True, exist_ok=True)
    # The backup folder that setup or restore makes the project, at its location after the move.
    adopted = None
    try:
        if adopt_backup:
            # The first change: everything above only read and validated.
            if args.dry_run:
                print(f"Move the backup to {project_dir / 'backup'} and make {project_dir} the project.")
                if assets.system_deployment_installed():
                    print(system_restore_hint(project_dir))
            else:
                try:
                    with project_lock(lock_path):
                        moved = transfer.adopt_bundle(project_dir)
                except OSError as exc:
                    raise transfer.TransferError(
                        f"Cannot make the backup folder {project_dir} the project: {exc}. Copy the folder to a "
                        "writable location, or run its loaf from the project directory.") from exc
                print(f"Moved the backup to {moved}. {project_dir} is the project now.", file=sys.stderr, flush=True)
                adopted = moved
                if archive is not None and archive.path.parent == project_dir:
                    archive = transfer.open_archive(moved, minimum=settings.backup_version_min, verify_launcher=False)
        if args.mode in INSTALL_MODES:
            return setup_command(
                args, setup_parts, project=project_dir, config_dir=config_dir, override_dir=override_dir,
                layer_dirs=layer_dirs, settings=settings, namespace=namespace, namespace_name=namespace_name,
                archive=archive, adopted=adopted, system=system_mode, standalone=standalone_setup,
            )
        compose = Compose(project_dir, files=compose_files, namespace=namespace, settings=settings, verbose=args.verbose,
                          build_apt_sources=args.with_apt_repos)
        if settings.terminal_server_enabled and args.mode in ("run", "restart"):
            if not compose.terminal_secret_file().exists():
                raise FileNotFoundError(
                    f"Terminal server key file is missing: {compose.terminal_secret_file()}. Create it with: "
                    + next_command(project_dir, override_dir, None, "setup", "terminal-server-key", system=system_mode))
            config.read_terminal_secret(compose.terminal_secret_file())
        if (settings.terminal_server_enabled and args.mode in ("run", "restart")) or args.with_apt_repos:
            # Refuse a missing or unusable APT setting before any service changes, also in dry runs.
            source, repository = compose.terminal_apt_inputs(serving=not args.with_apt_repos)
            if repository is not None:
                compose.repository_source(repository, source)
        if args.mode in ("build", "upgrade") and not args.with_apt_repos and settings.terminal_server_enabled and (
                settings.terminal_server_apt_source_file or settings.terminal_server_apt_repository_dir):
            print("Note: the terminal image builds with its base image's APT sources. Add --with-apt-repos to build "
                  "it with the [terminal_server] APT settings.", flush=True)
        if args.mode in TRANSFER_MODES:
            with (nullcontext() if args.dry_run or args.verify else project_lock(lock_path)):
                if args.verify:
                    if args.dry_run:
                        print(f"Verify the member hashes and report image platforms of {archive.path}")
                    else:
                        transfer.verify_backup(archive, namespace)
                    return 0
                if args.mode == "backup":
                    # Refuse unavailable types before the executable builds or services stop, also in dry runs.
                    transfer.check_backup_types(project_dir, config_dir, selected, terminal_key=compose.terminal_secret_file(),
                                                skip_terminal_server=args.no_terminal_server)
                    destination = transfer.backup_path(
                        (args.backup_dir or project_dir / settings.backup_dir).resolve(), namespace=namespace)
                    if set(transfer.SERVICE_TYPES) & set(selected):
                        steps.append(STOP_WITH_TERMINAL)
                    elif "models" in selected:
                        # A model snapshot tolerates the serving backend, not the downloader.
                        steps += [*(() if steps else (VALIDATE,)), STOP_DOWNLOADER]
                    # A checkout builds the executable that the folder carries; other installations need one.
                    script = package_script(project_dir, "binary")
                    executable = None if script is not None else transfer.release_executable(project_dir)
                    # Name the namespace in follow-up commands only when the project's settings select another one.
                    followup_name = None if namespace_name == settings.namespace_name else namespace_name
                    if args.dry_run:
                        print(f"Build the standalone executable at {project_dir / DIST_DIR / 'loaf'}" if script
                              else f"Include the standalone executable {executable}")
                        print(f"Save {', '.join(selected)} to {destination}")
                        if args.no_terminal_server:
                            print("Leave out the terminal server: its image, user volume and key")
                        elif "images" in selected:
                            print("Include the Open Terminal image if available locally")
                        execute_plan(compose, steps, lock_path=lock_path, dry_run=True)
                        if "models" in selected:
                            print("Check saved Modelfiles against the Ollama model store")
                    else:
                        if script is not None:
                            result = build_executable(project_dir, script, verbose=args.verbose)
                            if result:
                                return result
                            executable = project_dir / DIST_DIR / "loaf"

                        def prepare_services():
                            for step in steps:
                                compose.execute(step)

                        hashes = transfer.backup_bundle(
                            project_dir, destination, prepare_services, types=selected,
                            namespace_name=namespace_name, executable=executable,
                            compression_level=settings.compression_level, terminal_key=compose.terminal_secret_file(),
                            skip_terminal_server=args.no_terminal_server, config_dir=config_dir,
                        )
                        print(f"Backup saved to {destination}", flush=True)
                        print(transfer.file_hashes(hashes, created=True), flush=True)
                        if export_models or set(transfer.SERVICE_TYPES) & set(selected):
                            print("The backup stopped the deployment's services. Resume serving with: "
                                  + next_command(project_dir, override_dir, followup_name, "run", "offline",
                                                 system=system_mode), flush=True)
                elif args.dry_run:
                    print(f"Restore {', '.join(selected)} from {archive.path} into {project_dir}")
                    if archive.skip_terminal_server:
                        # The backup was made with --no-terminal-server.
                        if "config" in selected:
                            print("Disable the terminal server in the restored settings")
                        if "webui" in selected:
                            print("Remove loaf's saved terminal connection from the restored WebUI data")
                    if not system_mode and "config" not in selected and not project_ready(project_dir, config_dir):
                        print(f"Create {config_dir / config.CONFIG_NAME} and the model list in effect.")
                    execute_plan(compose, steps, lock_path=lock_path, dry_run=True)
                else:
                    restored = transfer.restore(
                        project_dir, archive, components, namespace=namespace, settings=settings,
                        configuration=(layer_dirs[:-1], config_dir, namespace_name), delete=args.delete,
                        system=system_mode,
                    )
                    if not system_mode and not project_ready(project_dir, config_dir):
                        # A project without settings gets the default ones, like after standalone setup.
                        setup.prepare_configuration(config_dir, layer_dirs[:-1],
                                                    None if settings.models_file is not None else "models.ini",
                                                    hint=False)
                    if restored.settings is not None:
                        compose = Compose(project_dir, files=compose_files, namespace=namespace, settings=restored.settings,
                                          verbose=args.verbose)
                    import_models = "models" in restored.types and (
                        compose.settings.import_models if args.import_models is None else args.import_models
                    )
                    workflow_mode = "offline" if import_models else "restore"
                    timeout = args.wait_timeout if args.wait_timeout is not None else compose.settings.wait_timeout
                    steps = plan("offline", wait_timeout=timeout, import_definitions=True, cpu=args.cpu) \
                        if import_models else []
                    print(f"Restored {', '.join(restored.types)} from {archive.path} into {project_dir}", flush=True)
                    for step in steps:
                        compose.execute(step)
                    if import_models:
                        backend = "CPU Ollama" if args.cpu else "GPU Ollama"
                        print(f"Modelfiles imported. {backend} is ready. Open WebUI is stopped.", flush=True)
                    # Name the namespace only when the project's settings would select another one.
                    followup_name = None if namespace_name == compose.settings.namespace_name else namespace_name
                    key = config.terminal_secret_path(project_dir, compose.settings.terminal_server_secret_file)
                    if compose.settings.terminal_server_enabled and not key.exists():
                        # offline would stop at the missing terminal key, which setup creates on request.
                        print("Next: " + next_command(project_dir, override_dir, None, "setup", "terminal-server-key",
                                                      system=system_mode), flush=True)
                    else:
                        print("Next: " + next_command(project_dir, override_dir, followup_name, "run", "offline",
                                                      *(("--cpu",) if args.cpu else ()), system=system_mode),
                              flush=True)
                    if adopted is not None and assets.system_deployment_installed():
                        print(system_restore_hint(adopted), flush=True)
            if args.attach:
                follow_logs(compose, workflow_mode, dry_run=args.dry_run, import_definitions=import_models)
            return 0
        if args.mode == "status":
            if args.dry_run:
                execute_plan(compose, steps, lock_path=lock_path, dry_run=True)
                gpu_check = "" if args.cpu else ", GPU access"
                print(f"Check Compose version, local images{gpu_check}, volumes and WebUI host ports")
            else:
                print(f"Deployment  {compose.project_name}", flush=True)
                print(f"Project     {project_dir}", flush=True)
                print(f"WebUI URL   {settings.webui_url()}", flush=True)
                report_settings(layer_dirs, namespace_name, config_dir,
                                project_dir / ".env" if settings.docker_env_enable else None)
                ready = readiness.check(compose, cpu=args.cpu)
                print("\nContainers across all profiles", flush=True)
                for step in steps:
                    compose.execute(step)
                print(f"\nPreflight {'passed' if ready else 'failed'}.", flush=True)
                if not ready:
                    return 1
            if args.attach:
                follow_logs(compose, args.mode, dry_run=args.dry_run)
            return 0
        if ask_docker:
            with project_lock(lock_path):
                state = serving_state(compose)
                if serving is None:
                    if len(state.profiles) != 1:
                        parser.error(("Both serving profiles have running containers. " if state.profiles else
                                      f"No serving profile of {namespace} runs. ") + "Name the profile to start, for "
                                     "example: " + next_command(project_dir, override_dir, args.namespace_name,
                                                                "restart", "offline", system=system_mode))
                    serving = workflow_mode = state.profiles[0]
                cpu = args.cpu or (args.mode == "restart" and state.cpu)
                compose = Compose(project_dir, files=(compose_file, *((cpu_file,) if cpu else ())), namespace=namespace,
                                  settings=settings, verbose=args.verbose)
                # The terminal server serves both profiles, so stopping one keeps it while the other one serves.
                other = next(profile for profile in SERVING if profile != serving)
                steps = plan(args.mode, wait_timeout=timeout, terminal_server=terminal,
                             remove_containers=remove_containers, cpu=cpu, serving=serving,
                             stop_terminal=other not in state.profiles)
                note_terminal_network(settings, args.mode, serving)
                execute_plan(compose, steps, lock_path=None)
        else:
            note_terminal_network(settings, args.mode, serving)
            execute_plan(compose, steps, lock_path=lock_path, dry_run=args.dry_run)
        if args.dry_run and args.mode in ("stop", "restart") and serving:
            if "terminal-server" in steps[1].arguments:
                other = next(profile for profile in SERVING if profile != serving)
                print(f"A real run keeps the terminal server running when the {other} profile serves.")
            if args.mode == "restart" and not cpu:
                print("A real run serves on CPU when Ollama serves on CPU now.")
        if not args.dry_run:
            # Name the namespace in follow-up commands only when the project's settings select another one.
            followup_name = None if namespace_name == settings.namespace_name else namespace_name
            if args.mode in ("run", "restart"):
                compute = " on CPU" if cpu else ""
                print(f"{serving.capitalize()} Ollama{compute} and Open WebUI are ready at {settings.webui_url()}", flush=True)
            elif args.mode in ("build", "upgrade"):
                print("Local images are ready.", flush=True)
            elif args.mode == "stop":
                scope = f"The {serving} profile's" if serving else "Deployment"
                message = (f"{scope} services are stopped and their containers removed." if remove_containers
                           else f"{scope} services are stopped.")
                print(message, flush=True)
            elif args.mode == "download":
                print("Downloads complete. Next: " + next_command(
                    project_dir, override_dir, followup_name, "run", "offline", system=system_mode), flush=True)
        if args.attach:
            follow_logs(compose, workflow_mode, dry_run=args.dry_run)
    except subprocess.CalledProcessError as exc:
        return command_failure(exc)
    except (OSError, config.ConfigError, AlreadyRunningError, setup.SetupError, transfer.TransferError,
            DockerQueryError, tarfile.TarError, EOFError, zlib.error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Conventional 128 + SIGINT; the lock context releases even on interruption.
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


