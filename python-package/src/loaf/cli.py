# cli.py - Cross-platform launcher behind the installed loaf command.
# Requires Python 3.10+ and Docker Compose; uses only the Python standard library.
# Keep planning separate from execution so dry runs and tests need no Docker daemon.
"""Local AI Ferry builds, runs and transfers local Ollama and Open WebUI deployments."""

from __future__ import annotations

import argparse
from configparser import ConfigParser, Error as INIError
from contextlib import nullcontext
from dataclasses import dataclass, field
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

from . import assets, cleanup, config, docs, integrations, readiness, setup, transfer
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
MODES = ("build", "upgrade", "download", "online", "offline", "status", "stop")
TRANSFER_MODES = ("backup", "restore")
# A backup folder is transfer media. Run from inside one, setup and restore use this project instead.
DEFAULT_PROJECT_DIR = Path("~/.local-ai-ferry")
INSTALL_MODES = ("setup", "install")
LINK_MODE = "link-docker-project"
# Tags may be quantization names or complete GGUF filenames. This conservative
# grammar checks syntax locally; repository/tag availability is a separate check.
MODEL_PATTERN = re.compile(
    r"hf\.co/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]*)?"
)
DEFAULT_MODEL_SECTION = "models.default"
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?")
TESTS_DIR = Path("python-package/tests")
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
) -> list[Step]:
    """Return a deterministic plan; all validation happens before Docker runs."""
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}")
    if wait_timeout < 1:
        raise ValueError("The wait timeout must be positive.")
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
    if mode == "stop":
        steps.append(STOP_ALL if terminal_server is None else STOP_WITH_TERMINAL)
        if remove_containers:
            steps.append(Step(profiles, ("rm", "--force"), "Remove stopped deployment containers"))
        return steps
    if mode == "status":
        return [Step(profiles, ("ps", "--all"), "List all deployment containers")]

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
    startup = (
        "up", "-d", "--no-build", "--pull", "never", "--wait",
        "--wait-timeout", str(wait_timeout),
    )
    backend = "Ollama on CPU" if cpu else "Ollama"
    description = {
        "download": "Start the model downloader",
        "online": f"Start {backend} and Open WebUI with internet access",
        "offline": f"Start offline {backend} and Open WebUI",
    }[profile]
    if import_definitions:
        description = f"Start offline {backend} and import saved Modelfiles"
    steps += [stop, Step((profile,), startup + targets, f"{description} and wait up to {wait_timeout}s for readiness")]
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
        namespace: str = "",
        settings: config.Settings | None = None,
        verbose: bool = False,
    ) -> None:
        # Resolve cwd when called, not when the package is imported or installed.
        self.project_dir = (project_dir or Path.cwd()).resolve()
        # Tests inject a generated Compose file and a unique project name while
        # production keeps stable resource names regardless of the caller's cwd.
        self.files = tuple(p.resolve() for p in (files or (assets.compose_path(self.project_dir),)))
        self.namespace = namespace
        self.project_name = project_name or namespace or "local-inference"
        self.settings = settings or config.Settings()
        self.verbose = verbose

    def command(self, step: Step) -> list[str]:
        command = [
            "docker", "compose", "--project-directory", str(self.project_dir),
            "--project-name", self.project_name,
        ]
        files = self.files
        if self.uses_terminal_runtime_source(step):
            files += (assets.DATA_DIR / "compose.terminal-apt.yml",)
        for path in files:
            command += ["--file", str(path)]
        for profile in step.profiles:
            command += ["--profile", profile]
        # Steps also name services explicitly: enabling a profile alone must not
        # accidentally start another service during a mode transition.
        return command + list(step.arguments)

    def variables(self, step: Step | None = None) -> dict[str, str]:
        variables = {"LOAF_NAMESPACE": self.namespace, **self.settings.compose_variables()}
        compose_dir = self.files[0].parent
        if compose_dir != self.project_dir:
            # Keep the working project's .env while resolving the relocated build contexts.
            variables["LOAF_DOCKER_DIR"] = str(compose_dir / "docker")
        if self.settings.cuda_visible_devices and "CUDA_VISIBLE_DEVICES" not in os.environ:
            variables["CUDA_VISIBLE_DEVICES"] = self.settings.cuda_visible_devices
        if self.settings.terminal_server_enabled:
            variables["LOAF_TERMINAL_SECRET_FILE"] = str(self.terminal_secret_file())
        apt_source = self.settings.terminal_server_apt_source_file
        if apt_source is not None and step is not None and "terminal-server" in step.arguments:
            runtime = self.uses_terminal_runtime_source(step)
            if runtime or step.arguments[0] == "build":
                path = self.terminal_apt_source_file(apt_source)
                variables.update(LOAF_TERMINAL_APT_SOURCE_FILE=str(path),
                                 LOAF_TERMINAL_APT_SOURCE_FORMAT=apt_source.suffix[1:],
                                 LOAF_TERMINAL_APT_SOURCE_HASH=setup.file_digest(path))
            if runtime:
                variables["LOAF_TERMINAL_RUNTIME_DIR"] = str(assets.DATA_DIR / "docker/terminal-server")
                repository = self.terminal_apt_repository_dir()
                if repository is not None:
                    variables["LOAF_TERMINAL_APT_REPOSITORY_DIR"] = str(repository)
        return variables

    def terminal_secret_file(self) -> Path:
        return config.terminal_secret_path(self.project_dir, self.settings.terminal_server_secret_file)

    def uses_terminal_runtime_source(self, step: Step) -> bool:
        return (self.settings.terminal_server_enabled and self.settings.terminal_server_apt_source_file is not None
                and step.arguments[0] == "up" and "terminal-server" in step.arguments)

    def terminal_apt_source_file(self, configured: Path) -> Path:
        source = (self.project_dir / configured.expanduser()).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Terminal server APT source file is missing: {source}")
        return source

    def terminal_apt_repository_dir(self) -> Path | None:
        configured = self.settings.terminal_server_apt_repository_dir
        if configured is None:
            return None
        directory = (self.project_dir / configured.expanduser()).resolve()
        if not directory.is_dir():
            raise FileNotFoundError(f"Terminal server APT repository directory is missing: {directory}")
        # A flat repository has its index at the root, a suite repository below dists/.
        indexes = ("dists", "Release", "InRelease", *(f"Packages{suffix}" for suffix in ("", ".gz", ".xz", ".bz2",
                                                                                     ".lz4", ".zst")))
        if not any((directory / name).exists() for name in indexes):
            raise FileNotFoundError(
                f"Terminal server APT repository directory has no APT index (Packages, Release or dists/): {directory}")
        return directory

    def environment(self, variables: dict[str, str]) -> dict[str, str]:
        return {**os.environ, **variables}

    def trace(self, step: Step, variables: dict[str, str] | None = None) -> None:
        defaults = config.Settings().compose_variables()
        assignments = []
        values = self.variables(step) if variables is None else variables
        for name, value in values.items():
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
        variables = self.variables(step)
        if self.verbose:
            self.trace(step, variables)
        elif step.description and action not in ("config", "ps"):
            print(f"{step.description}…", file=sys.stderr, flush=True)
        env = self.environment(variables)
        wait = action == "up" and "--wait" in step.arguments
        with (wait_status("Waiting for services to become healthy") if wait else nullcontext()):
            subprocess.run(self.command(step), cwd=self.project_dir, check=True, shell=False, env=env)


def execute_plan(
    compose: Compose, steps: Sequence[Step], *, lock_path: Path, dry_run: bool = False
) -> None:
    if dry_run:
        # No lock creation or Docker probing: preview works on an unprepared host.
        if steps and not compose.verbose:
            print(f"Plan for {compose.project_dir} ({compose.project_name})")
        for step in steps:
            compose.preview(step)
        return
    # Hold one lock across the whole transition. check=True stops the sequence at
    # its first failure; successful downloads/exports remain available to retry.
    with project_lock(lock_path):
        for step in steps:
            compose.execute(step)


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
DEPLOYMENT_OPTIONS = ("--config-dir", "--version", "--dry-run")
COMPOSE_OPTIONS = (*DEPLOYMENT_OPTIONS, "--attach")
START_OPTIONS = (*COMPOSE_OPTIONS, "--wait-timeout")
TRANSFER_OPTIONS = (*DEPLOYMENT_OPTIONS, "--backup-dir", "--archive")
SETUP_OPTIONS = ("--config-dir", "--dry-run", "--archive", "--with-docker-deps", "--terminal-server-key",
                 "--test-local-repository")
SETUP_NOTES = (
    "Run inside a copied backup folder to prepare ~/.local-ai-ferry from it.\n"
    "Run 'setup tests' with the checkout's virtualenv to cache and install the test packages."
)
COMMAND_HELP = {
    "setup": CommandHelp("Prepare a deployment or install loaf from source.", SETUP_OPTIONS, SETUP_NOTES),
    "install": CommandHelp("Alias for setup.", SETUP_OPTIONS, SETUP_NOTES),
    "binary": CommandHelp("Build the standalone executable at dist/loaf.", ("--dry-run",)),
    "clean": CommandHelp("Remove generated temporary files and the runtime cache.", ("--dry-run", "--full")),
    "link-docker-project": CommandHelp("Link the packaged Docker project for direct Compose use.", ("--dry-run",)),
    "version": CommandHelp("Show the loaf release, installation and default deployment."),
    "docs": CommandHelp(
        "List the bundled guides or show one.", (),
        "Run 'loaf docs NAME' to read a guide. 'loaf docs loaf.1' prints the manual page:\n"
        "loaf docs loaf.1 | man -l -",
    ),
    "build": CommandHelp("Build the local deployment images.", COMPOSE_OPTIONS),
    "upgrade": CommandHelp("Pull updated base images and rebuild local images.", COMPOSE_OPTIONS),
    "download": CommandHelp("Pull the selected Ollama models.", (*START_OPTIONS, "--models-file")),
    "online": CommandHelp("Start Ollama and WebUI with internet access.", (*START_OPTIONS, "--cpu")),
    "offline": CommandHelp("Start Ollama and WebUI for disconnected use.", (*START_OPTIONS, "--cpu")),
    "status": CommandHelp("Check deployment readiness and list containers.", (*COMPOSE_OPTIONS, "--cpu")),
    "stop": CommandHelp(
        "Stop deployment services and remove their containers.", (*COMPOSE_OPTIONS, "--remove-containers", "-n"),
    ),
    "backup": CommandHelp(
        "Save the deployment in a portable backup folder.",
        (*TRANSFER_OPTIONS, "--verify", "--no-terminal-server", "--export", "--models-file", "--wait-timeout",
         "--attach"),
        "Types are source, config, installers, images, models, webui and chats.\n"
        "project means config and installers; volumes means models, webui and chats.\n"
        "chats adds webui. Omit types to save all available ones. Use --archive with --verify.",
    ),
    "restore": CommandHelp(
        "Restore selected types from a backup.",
        (*TRANSFER_OPTIONS, "--delete", "--import", "--cpu", "--wait-timeout", "--attach"),
        "Types are those of backup. Omit them to restore everything in the archive.\n"
        "models merge into the model store; add --delete to mirror them. webui and\n"
        "chats need new or empty volumes. source needs a project without its files.",
    ),
    "integration": CommandHelp("Print a host integration."),
    "test": CommandHelp(
        "Run the checkout's verification suite.", ("--config-dir", "--cpu", "--badges"),
        "Use --cpu to run the Docker and model workflow tests with CPU inference.",
    ),
}
INTEGRATION_HELP = {
    "systemd": CommandHelp("Print a systemd unit for offline serving.", ("--config-dir", "--version")),
    "continue": CommandHelp("Print Continue YAML for the selected models.", ("--config-dir", "--models-file")),
    "tether": CommandHelp("Print Open WebUI Tether installation instructions."),
}
CLI_USAGE = "%(prog)s [GLOBAL OPTIONS] COMMAND [OPTIONS]"


def add_options(parser, selected: tuple[str, ...] | None = None) -> None:
    def add(*flags, **settings):
        if selected is None or flags[-1] in selected:
            parser.add_argument(*flags, **settings)

    add("-h", "--help", action="store_true", help="Show help and exit.")
    add("--project-dir", type=Path, metavar="PATH",
        help="Use PATH as the project (default: current directory; inside a backup folder, ~/.local-ai-ferry).")
    add("--system", action="store_true", help="Use the system deployment: settings in /etc/loaf, data in /var/lib/loaf.")
    add("-v", "--verbose", action="store_true", help="Show external commands or individual test names.")
    add("--config-dir", type=Path, metavar="PATH", help="Use PATH for settings (default: project config/).")
    add("--version", metavar="VERSION", help="Select a deployment namespace. Use --version= to clear it.")
    add("--dry-run", action="store_true", help="Preview without running Docker or changing the project.")
    add("--models-file", type=Path, metavar="PATH", help="Select the model INI for this run.")
    add("--backup-dir", type=Path, metavar="PATH", help="Override the configured archive directory.")
    add("--archive", type=Path, metavar="PATH", help="Select a backup folder or archive file.")
    add("--verify", action="store_true", help="Check an existing backup's hashes and image platforms without Docker.")
    add("--no-terminal-server", action="store_true", help="Skip the locally available Open Terminal image.")
    add("--delete", action="store_true", help="Mirror the archived models: delete destination model data absent from it.")
    add("--export", dest="export_models", action=argparse.BooleanOptionalAction,
        help="Enable or disable exporting selected Modelfiles from the cached models before backup.")
    add("--import", dest="import_models", action=argparse.BooleanOptionalAction,
        help="Enable or disable starting offline Ollama to apply saved Modelfiles after restore.")
    add("--remove-containers", action=argparse.BooleanOptionalAction,
        help="Remove stopped service containers by default, or keep them with --no-remove-containers.")
    add("-n", dest="remove_containers", action="store_false", default=None, help="Keep stopped service containers and logs.")
    add("--cpu", action="store_true", help="Use CPU inference instead of the default NVIDIA GPU.")
    add("--with-docker-deps", action="store_true",
        help="On Debian 12 or 13, cache Docker Engine, Compose and Buildx packages and install them as needed.")
    add("--terminal-server-key", action="store_true", help="Create the terminal server key only.")
    add("--test-local-repository", action="store_true",
        help="Download a few Debian command-line tools into a local test APT repository and list them.")
    add("--badges", action="store_true", help="Record test results and update badges for the groups that ran.")
    add("--full", action="store_true", help="Also remove generated installation files.")
    add("--wait-timeout", type=int, metavar="SECONDS", help="Override the service readiness wait from config.ini.")
    add("-a", "--attach", action="store_true", help="Follow live Compose logs after the workflow.")


def help_listing(heading: str, commands: dict[str, CommandHelp]) -> str:
    width = max(map(len, commands))
    rows = (f"  {name:<{width}}  {details.summary}" for name, details in commands.items())
    return "\n".join((heading, *rows))


def show_help(command: str | None, integration: str | None) -> None:
    prog = " ".join(part for part in ("loaf", command, integration) if part)
    usage = "%(prog)s [OPTIONS]"
    options = ()
    if command is None:
        usage = CLI_USAGE
        description = f"{__doc__}\n\n{help_listing('Commands', COMMAND_HELP)}"
        notes = "Run 'loaf COMMAND --help' for command options.\nSee docs/cli-help.md for the full reference."
    elif command == "integration" and integration is None:
        usage = "%(prog)s INTEGRATION [OPTIONS]"
        description = f"{COMMAND_HELP[command].summary}\n\n{help_listing('Integrations', INTEGRATION_HELP)}"
        notes = "Run 'loaf integration INTEGRATION --help' for integration options."
    else:
        details = COMMAND_HELP[command] if integration is None else INTEGRATION_HELP[integration]
        description, options, notes = details.summary, details.options, details.notes
        if command in TRANSFER_MODES:
            usage = "%(prog)s [OPTIONS] [TYPE ...]"
        elif command in INSTALL_MODES:
            usage = "%(prog)s [OPTIONS] [tests]"
        elif command == "docs":
            usage = "%(prog)s [NAME]"
    parser = argparse.ArgumentParser(
        prog=prog, usage=usage, description=description, epilog=notes, add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    if options:
        add_options(parser.add_argument_group("Options"), options)
    add_options(parser.add_argument_group("Global options"), GLOBAL_OPTIONS)
    parser.print_help()
    parser.exit()


def project_command(project: Path, config_dir: Path | None, version: str | None, *, system: bool = False) -> list[str]:
    executable = os.environ.get("LOAF_RELEASE_EXECUTABLE")
    if executable and setup.bundled_release() is not None and transfer.standalone_executable(Path(executable)):
        command = [executable]
    else:
        # Isolated mode keeps a writable working directory out of the module path.
        command = [sys.executable, *(("-I",) if system else ()), "-m", "loaf"]
    command.extend(("--system",) if system else ("--project-dir", str(project)))
    if config_dir is not None:
        command.extend(("--config-dir", str(config_dir)))
    if version:
        command.extend(("--version", version))
    elif version is not None:
        command.append("--version=")
    return command


def binary_builder(project: Path) -> Path | None:
    script = project / "scripts/build-release.sh"
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


def report_settings(layers: Sequence[Path], version: str, config_dir: Path) -> None:
    files = [str(directory / config.CONFIG_NAME) for directory in layers[1:] if (directory / config.CONFIG_NAME).is_file()]
    print("Settings    " + ", ".join(["packaged defaults", *files]), flush=True)
    warn_shared_deployment(layers, version, config_dir)


def warn_shared_deployment(layers: Sequence[Path], version: str, config_dir: Path) -> None:
    shared = config.shared_system_deployment(tuple(layers), version)
    if shared is not None:
        print(
            f"Warning: this project and the system deployment in {shared} use the same Docker namespace "
            f"{config.namespace(version) or 'local-inference'}. Set [launcher] version in "
            f"{config_dir / config.CONFIG_NAME} for a separate deployment, or set system_config = to skip "
            "the system settings.", file=sys.stderr, flush=True,
        )


def build_executable(project: Path, script: Path, *, verbose: bool) -> int:
    command = ["bash", str(script), "--binary-only"]
    if verbose:
        print("+ " + display_command(command), flush=True)
    env = {**os.environ, "LOAF_BUILD_PYTHON": sys.executable}
    return subprocess.run(command, cwd=project, env=env, check=False).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loaf", usage=CLI_USAGE, add_help=False)
    parser.add_argument("mode", nargs="?", choices=COMMAND_HELP)
    parser.add_argument("arguments", nargs="*")
    add_options(parser)
    args = parser.parse_intermixed_args(argv)
    integration = None
    components = tuple(args.arguments) if args.mode in TRANSFER_MODES and args.arguments else None
    setup_tests = args.mode in INSTALL_MODES and args.arguments == ["tests"]
    docs_name = args.arguments[0] if args.mode == "docs" and len(args.arguments) == 1 else None
    if args.mode == "integration" and args.arguments:
        if len(args.arguments) != 1 or args.arguments[0] not in INTEGRATION_HELP:
            parser.error("invalid choice for integration. Use systemd, continue or tether.")
        integration = args.arguments[0]
    elif components is None and args.arguments and not setup_tests and docs_name is None:
        if args.arguments[0] in INTEGRATION_HELP:
            parser.error("Use the integration name with loaf integration.")
        if args.mode in INSTALL_MODES:
            parser.error(f"{args.mode} accepts only the tests argument. See 'loaf {args.mode} --help'.")
        parser.error(f"Unexpected arguments for {args.mode}. See 'loaf {args.mode} --help'.")
    if args.help:
        show_help(args.mode, integration)
    if args.mode is None:
        parser.error("A command is required. See 'loaf --help'.")
    if args.mode == "integration" and integration is None:
        parser.error("integration requires a name: systemd, continue or tether.")
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
    if args.mode == "backup" and args.no_terminal_server and named and "images" not in named:
        parser.error("--no-terminal-server is only supported when creating a backup that includes images.")
    if args.mode == "backup" and args.export_models and named and "models" not in named:
        parser.error("--export requires the models type.")
    if args.mode == "restore" and named and "models" not in named:
        if args.delete:
            parser.error("--delete applies to the models type of restore.")
        if args.import_models:
            parser.error("--import requires the models type.")
    if args.remove_containers is not None and args.mode != "stop":
        parser.error("--remove-containers, --no-remove-containers and -n are only supported by stop.")
    remove_containers = args.remove_containers is not False
    if args.verify and args.mode != "backup":
        parser.error("--verify is only supported by backup.")
    if args.verify and components:
        parser.error("--verify checks the whole archive. Omit the types.")
    if args.no_terminal_server and (args.mode != "backup" or args.verify):
        parser.error("--no-terminal-server is only supported when creating a backup that includes images.")
    if args.delete and args.mode != "restore":
        parser.error("--delete is only supported by restore, for the models type.")
    if args.cpu and args.mode not in ("online", "offline", "status", "restore", "test"):
        parser.error("--cpu is only supported by online, offline, status, restore and test.")
    if args.with_docker_deps and args.mode not in INSTALL_MODES:
        parser.error("--with-docker-deps is only supported by setup and install.")
    if (args.terminal_server_key or args.test_local_repository) and args.mode not in INSTALL_MODES:
        parser.error("--terminal-server-key and --test-local-repository are only supported by setup and install.")
    if args.terminal_server_key and (args.with_docker_deps or args.archive is not None or args.test_local_repository):
        parser.error("--terminal-server-key is for key setup only.")
    if args.test_local_repository and (args.with_docker_deps or args.archive is not None):
        parser.error("--test-local-repository only creates the test repository. Omit --with-docker-deps and --archive.")
    if setup_tests and (args.terminal_server_key or args.with_docker_deps or args.archive is not None
                        or args.test_local_repository):
        parser.error("setup tests installs only the test packages. "
                     "Omit --terminal-server-key, --test-local-repository, --with-docker-deps and --archive.")
    if args.badges and args.mode != "test":
        parser.error("--badges is only supported by test.")
    if args.full and args.mode != "clean":
        parser.error("--full is only supported by clean.")
    if args.backup_dir is not None and args.mode not in TRANSFER_MODES:
        parser.error("--backup-dir is only supported by backup and restore.")
    if args.attach and (args.verify or args.mode not in (*MODES, *TRANSFER_MODES)):
        parser.error("--attach is available for Compose and transfer commands.")
    if args.system and (args.project_dir is not None or args.config_dir is not None):
        parser.error("--system uses /etc/loaf and /var/lib/loaf. Omit --project-dir and --config-dir.")
    unexpected = any(value is not None for value in (
        args.config_dir, args.version, args.models_file, args.backup_dir, args.archive,
        args.export_models, args.import_models, args.wait_timeout,
    ))
    if args.mode in ("version", "docs"):
        if unexpected or args.dry_run or args.project_dir is not None:
            parser.error(f"Unexpected option for {args.mode}. See 'loaf {args.mode} --help'.")
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
        setup_tests or args.test_local_repository or args.with_docker_deps or args.archive is not None
    ):
        parser.error("The system deployment uses the Debian package's dependencies. Omit tests, "
                     "--test-local-repository, --with-docker-deps and --archive, or select a project with --project-dir.")
    if system_mode:
        project_dir = assets.SYSTEM_PROJECT_DIR
        if not project_dir.is_dir():
            print(f"Error: The system deployment directory {project_dir} is missing. "
                  "Install the Debian package, or omit --system.", file=sys.stderr)
            return 1
        cwd = Path.cwd()
        if not args.system and ((cwd / "config" / config.CONFIG_NAME).is_file() or (cwd / assets.DOCKER_PROJECT).is_symlink()):
            print(f"Using the system deployment in {project_dir}. Add --project-dir . to use this directory.",
                  file=sys.stderr, flush=True)
    else:
        project_dir = (args.project_dir or Path.cwd()).resolve()
        if args.project_dir is None and transfer.is_bundle(project_dir):
            project_dir = DEFAULT_PROJECT_DIR.expanduser()
            print(f"Using the project {project_dir}. The backup folder stays unchanged. "
                  "Select another project with --project-dir.", file=sys.stderr, flush=True)
        elif transfer.is_bundle(project_dir) and not args.verify:
            parser.error(f"{project_dir} is a backup folder, which loaf leaves unchanged. Select another --project-dir.")
        if args.mode in (*INSTALL_MODES, "restore") and not args.dry_run:
            project_dir.mkdir(parents=True, exist_ok=True)
    if args.mode in (LINK_MODE, "binary", "clean") or setup_tests:
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
                cleanup.clean(project_dir, full=args.full, dry_run=args.dry_run)
                return 0
            if args.mode == LINK_MODE:
                assets.link_docker_project(project_dir, dry_run=args.dry_run)
                return 0
            script = binary_builder(project_dir)
            if script is None:
                parser.error("binary requires the loaf source directory. Select it with --project-dir.")
            if args.dry_run:
                print(f"Build the standalone executable at {project_dir / 'dist/loaf'}.")
                return 0
            with project_lock(project_dir / PROJECT_LOCK):
                return build_executable(project_dir, script, verbose=args.verbose)
        except (OSError, config.ConfigError, setup.SetupError, AlreadyRunningError, subprocess.CalledProcessError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    compose_file = assets.DATA_DIR / "compose.yml" if system_mode else assets.compose_path(project_dir)
    if args.mode == "restore" and not compose_file.is_file():
        compose_file = assets.DATA_DIR / "compose.yml"
    data_dir = assets.DATA_DIR if compose_file.parent == project_dir else compose_file.parent
    defaults_dir = data_dir / "config"
    terminal_available = (compose_file.parent / "docker/terminal-server/Dockerfile").is_file()
    if system_mode:
        # The system deployment's own settings are its top layer.
        config_dir = config.system_directory() or config.SYSTEM_CONFIG_DIR
        layer_dirs = (defaults_dir, config_dir)
    else:
        config_dir = args.config_dir.resolve() if args.config_dir else project_dir / "config"
        layer_dirs = ()
    if args.config_dir is not None and not config_dir.is_dir():
        parser.error(f"Configuration directory does not exist: {config_dir}.")
    standalone_setup = args.mode in INSTALL_MODES and setup.bundled_release() is not None and not system_mode
    if args.backup_dir is not None and args.archive is not None:
        parser.error("Use either --backup-dir or --archive.")
    if args.archive is not None and args.mode != "restore" and not (args.verify or standalone_setup):
        parser.error("--archive is for standalone setup, restore or backup --verify.")
    archive = None
    backup_dir = None
    try:
        layer_dirs = layer_dirs or config.layers(defaults_dir, config_dir)
        settings = config.load(*layer_dirs)
        version = settings.version if args.version is None else args.version
        namespace = config.namespace(version)
        if args.mode == "restore" or args.verify or standalone_setup or args.terminal_server_key:
            # An explicit archive, else the backup folder this runs in or from, else the newest backup.
            selection = args.archive.resolve() if args.archive is not None else None
            if selection is None and args.backup_dir is None:
                selection = transfer.find_bundle(Path.cwd(), None)
            if selection is None and (args.mode == "restore" or args.verify):
                backup_dir = (args.backup_dir or project_dir / settings.backup_dir).resolve()
                selection = transfer.latest_backup(backup_dir, namespace=namespace)
                if selection is None and not args.dry_run:
                    raise transfer.TransferError(f"No backup found in {backup_dir}. Use --backup-dir or --archive.")
            if selection is not None:
                archive = transfer.open_archive(selection, minimum=settings.backup_version_min,
                                                verify_launcher=not args.dry_run)
                if args.version is None or args.version == archive.deployment:
                    version = archive.deployment
                    namespace = config.namespace(version)
                elif not (args.mode == "restore" and named and set(named) <= set(transfer.PORTABLE_TYPES)):
                    raise transfer.TransferError(
                        f"Backup version is {archive.deployment or 'unversioned'}. Select the matching --version, "
                        f"or restore only {', '.join(transfer.PORTABLE_TYPES)} into another deployment.")
        selected = ()
        if args.mode == "backup" and not args.verify:
            selected = (transfer.expand_types(components, chats_available=settings.terminal_server_enabled)
                        if components else
                        transfer.default_backup_types(project_dir, chats=settings.terminal_server_include_in_backup))
        elif args.mode == "restore":
            selected = (transfer.expand_types(components) if components else
                        tuple(kind for kind in transfer.TYPES if archive is None or kind in archive.items))
    except (OSError, transfer.TransferError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except config.ConfigError as exc:
        parser.error(str(exc))
    except ValueError as exc:
        parser.error(f"--version: {exc}.")
    backup_models = args.mode == "backup" and "models" in selected
    restore_models = args.mode == "restore" and "models" in selected
    if args.no_terminal_server and "images" not in selected:
        parser.error("--no-terminal-server is only supported when creating a backup that includes images.")
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
    workflow_mode = args.mode
    if export_models:
        workflow_mode = "download"
    elif import_models:
        workflow_mode = "offline"
    if (args.models_file is not None and not export_models
            and args.mode not in ("download", "offline") and not continue_integration):
        parser.error("--models-file is supported by download, backup --export and integration continue.")
    if args.wait_timeout is not None and workflow_mode not in SERVICES:
        parser.error("--wait-timeout is only supported by commands that start services.")
    if args.mode == "test":
        if args.dry_run or args.version is not None:
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
            or standalone_setup or args.terminal_server_key or args.test_local_repository or compose_file.is_file()):
        parser.error(f"No compose.yml in {project_dir}. Run loaf setup or select a project with --project-dir.")
    compose_files = (compose_file,)
    if args.cpu:
        cpu_file = data_dir / "compose.cpu.yml"
        if not cpu_file.is_file():
            parser.error(f"The CPU Compose override is missing: {cpu_file}.")
        compose_files += (cpu_file,)
    if args.mode in INSTALL_MODES and args.version is not None:
        parser.error(f"{args.mode} prepares the checkout and host. Omit --version.")
    override_dir = config_dir if args.config_dir is not None else None
    lock_path = project_dir / PROJECT_LOCK
    steps = []
    try:
        if integration == "systemd":
            if sys.platform != "linux":
                parser.error("integration systemd requires Linux.")
            launcher = project_command(project_dir, override_dir, version, system=system_mode)
            print(integrations.systemd_unit(project_dir, launcher, 0 if system_mode else os.getuid()), end="")
            return 0
        models = []
        if workflow_mode == "download" or continue_integration:
            models_file = select_models_file(layer_dirs, args.models_file, settings.models_file)
            models = read_models(models_file)
        if continue_integration:
            print(integrations.continue_config(settings.webui_url(), ((m.name, m.source) for m in models)), end="")
            return 0
        if workflow_mode in MODES and (args.mode != "restore" or args.dry_run):
            if settings.terminal_server_enabled and workflow_mode in ("build", "upgrade", "online", "offline"):
                if not terminal_available:
                    parser.error("The selected Compose project does not include the bundled terminal server.")
                if args.mode in ("online", "offline") and not settings.auth:
                    parser.error("The terminal server requires [webui] auth = true.")
            timeout = args.wait_timeout if args.wait_timeout is not None else settings.wait_timeout
            steps = plan(
                workflow_mode, models, wait_timeout=timeout,
                export_definitions=export_models, import_definitions=import_models,
                terminal_server=settings.terminal_server_enabled if terminal_available else None,
                remove_containers=remove_containers, cpu=args.cpu,
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        if args.mode in INSTALL_MODES:
            key_path = config.terminal_secret_path(project_dir, settings.terminal_server_secret_file)
            if system_mode:
                if args.dry_run:
                    print(f"Create the terminal server key at {key_path} if missing.")
                else:
                    with project_lock(lock_path):
                        setup.ensure_terminal_key(key_path, group=setup.SYSTEM_GROUP)
                    print(f"System deployment ready: settings in {config_dir}, data in {project_dir}.")
                return 0
            if args.test_local_repository:
                image = transfer.image_names(namespace, include_terminal=True)[-1]
                if args.dry_run:
                    print(f"Download a few Debian command-line tools with {image} into "
                          f"{project_dir / setup.TEST_REPOSITORY_DIR} and write {project_dir / setup.TEST_REPOSITORY_SOURCE}.")
                else:
                    with project_lock(lock_path):
                        setup.create_test_repository(project_dir, image)
                return 0
            if args.terminal_server_key:
                if args.dry_run:
                    print(f"Create the terminal server key at {key_path} if missing.")
                else:
                    with project_lock(lock_path):
                        archived_key = None
                        if archive is not None and not key_path.exists():
                            archived_key = transfer.setup_from_archive(archive, project_dir, installers=False)
                        setup.ensure_terminal_key(key_path, archived_key)
                return 0
            if standalone_setup:
                if args.dry_run:
                    print(f"Prepare default configuration and link the packaged Docker project in {project_dir}.")
                    print(f"Create the terminal server key at {key_path} if missing.")
                    if args.with_docker_deps:
                        print("Prepare Docker host packages" + (f" from {archive.path}." if archive else "."))
                else:
                    with project_lock(lock_path):
                        archived_key = None
                        if archive is not None and (args.with_docker_deps or not key_path.exists()):
                            archived_key = transfer.setup_from_archive(archive, project_dir, installers=args.with_docker_deps)
                        if args.with_docker_deps:
                            if archive is not None:
                                setup.install_backup_host_packages(project_dir)
                            else:
                                setup.prepare_docker(project_dir)
                                setup.install_toolkit(project_dir)
                        setup.setup_standalone(project_dir)
                        setup.ensure_terminal_key(key_path, archived_key)
                        launcher = project_dir / transfer.LAUNCHER
                        if archive is not None and archive.executable is not None and not (
                                launcher.exists() or launcher.is_symlink()):
                            # Keep a launcher in the project; the backup folder can stay on its media.
                            shutil.copyfile(archive.executable, launcher)
                            launcher.chmod(transfer.file_modes(project_dir)[1])
                        print(f"Deployment ready in {project_dir}.")
                    warn_shared_deployment(layer_dirs, version, config_dir)
                return 0
            if args.dry_run:
                if args.with_docker_deps:
                    print("Cache and, if needed, install Debian Docker Engine, Compose and Buildx packages.")
                print(f"Prepare {project_dir / '.venv'}, cache Python wheels and prepare the Debian NVIDIA toolkit.")
                print(f"Create the terminal server key at {key_path} if missing.")
            else:
                with project_lock(lock_path):
                    try:
                        setup.install(project_dir, with_docker_deps=args.with_docker_deps)
                        setup.ensure_terminal_key(key_path)
                        warn_shared_deployment(layer_dirs, version, config_dir)
                    except (OSError, setup.SetupError, subprocess.CalledProcessError):
                        print("See docs/debian.md for Debian host setup.", file=sys.stderr)
                        raise
            return 0
        compose = Compose(project_dir, files=compose_files, namespace=namespace, settings=settings, verbose=args.verbose)
        if settings.terminal_server_enabled and args.mode in ("online", "offline") and not args.dry_run:
            config.read_terminal_secret(compose.terminal_secret_file())
            if settings.terminal_server_apt_source_file is not None:
                compose.terminal_apt_source_file(settings.terminal_server_apt_source_file)
                compose.terminal_apt_repository_dir()
        if args.mode in TRANSFER_MODES:
            with (nullcontext() if args.dry_run or args.verify else project_lock(lock_path)):
                if args.verify:
                    if archive is None:
                        print(f"Verify the newest backup in {backup_dir}")
                    elif args.dry_run:
                        print(f"Verify the member hashes and report image platforms of {archive.path}")
                    else:
                        transfer.verify_backup(archive, namespace)
                    return 0
                if args.mode == "backup":
                    destination = transfer.backup_path(
                        (args.backup_dir or project_dir / settings.backup_dir).resolve(), namespace=namespace)
                    if set(transfer.SERVICE_TYPES) & set(selected):
                        steps.append(STOP_WITH_TERMINAL if terminal_available else STOP_ALL)
                    elif "models" in selected:
                        # A model snapshot tolerates the serving backend, not the downloader.
                        steps += [*(() if steps else (VALIDATE,)), STOP_DOWNLOADER]
                    script = binary_builder(project_dir)
                    if args.dry_run:
                        if script is not None:
                            print(f"Build the standalone executable at {project_dir / 'dist/loaf'}")
                        print(f"Save {', '.join(selected)} to {destination}")
                        if "images" in selected:
                            print("Skip the Open Terminal image" if args.no_terminal_server else
                                  "Include the Open Terminal image if available locally")
                        execute_plan(compose, steps, lock_path=lock_path, dry_run=True)
                        if "models" in selected:
                            print("Check saved Modelfiles against the Ollama model store")
                    else:
                        executable = None
                        if script is not None:
                            result = build_executable(project_dir, script, verbose=args.verbose)
                            if result:
                                return result
                            executable = project_dir / "dist/loaf"

                        def prepare_services():
                            for step in steps:
                                compose.execute(step)

                        transfer.backup_bundle(
                            project_dir, destination, prepare_services, types=selected,
                            version=version, executable=executable, compression_level=settings.compression_level,
                            terminal_key=compose.terminal_secret_file(), include_terminal_image=not args.no_terminal_server,
                            config_dir=config_dir,
                        )
                        print(f"Backup saved to {destination}", flush=True)
                elif args.dry_run:
                    source = archive.path if archive is not None else f"the newest backup in {backup_dir}"
                    print(f"Restore {', '.join(selected)} from {source} into {project_dir}")
                    execute_plan(compose, steps, lock_path=lock_path, dry_run=True)
                else:
                    restored = transfer.restore(
                        project_dir, archive, components, namespace=namespace, settings=settings,
                        configuration=(layer_dirs[:-1], config_dir, version), delete=args.delete,
                    )
                    if restored.settings is not None:
                        compose = Compose(project_dir, files=compose_files, namespace=namespace, settings=restored.settings,
                                          verbose=args.verbose)
                    import_models = "models" in restored.types and (
                        compose.settings.import_models if args.import_models is None else args.import_models
                    )
                    workflow_mode = "offline" if import_models else "restore"
                    steps = plan("offline", wait_timeout=args.wait_timeout or compose.settings.wait_timeout,
                                 import_definitions=True, cpu=args.cpu) if import_models else []
                    print(f"Restored {', '.join(restored.types)} from {archive.path} into {project_dir}", flush=True)
                    for step in steps:
                        compose.execute(step)
                    if import_models:
                        backend = "CPU Ollama" if args.cpu else "GPU Ollama"
                        print(f"Modelfiles imported. {backend} is ready. Open WebUI is stopped.", flush=True)
                    if project_dir != Path.cwd().resolve():
                        followup = project_command(project_dir, override_dir, version)
                        print("Next: " + display_command(followup + ["offline"]), flush=True)
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
                report_settings(layer_dirs, version, config_dir)
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
        execute_plan(compose, steps, lock_path=lock_path, dry_run=args.dry_run)
        if not args.dry_run:
            # Pin the interpreter and local options in the next command.
            followup = project_command(project_dir, override_dir, version or args.version)
            if args.mode in ("online", "offline"):
                compute = " on CPU" if args.cpu else ""
                print(f"{args.mode.capitalize()} Ollama{compute} and Open WebUI are ready at {settings.webui_url()}", flush=True)
            elif args.mode in ("build", "upgrade"):
                print("Local images are ready.", flush=True)
            elif args.mode == "stop":
                message = ("Stopped deployment containers removed." if remove_containers
                           else "Deployment services are stopped.")
                print(message, flush=True)
            elif args.mode == "download":
                print("Downloads complete. Next: " + display_command(followup + ["offline"]), flush=True)
        if args.attach:
            follow_logs(compose, workflow_mode, dry_run=args.dry_run)
    except subprocess.CalledProcessError as exc:
        program = exc.cmd if isinstance(exc.cmd, str) else exc.cmd[0]
        print(f"{program} failed (exit {exc.returncode}).", file=sys.stderr)
        # Negative signal statuses and shell-reserved exit values become a stable
        # generic failure; ordinary Docker failures retain their useful exit code.
        return exc.returncode if 0 < exc.returncode < 126 else 1
    except (OSError, config.ConfigError, AlreadyRunningError, setup.SetupError, transfer.TransferError,
            tarfile.TarError, EOFError, zlib.error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Conventional 128 + SIGINT; the lock context releases even on interruption.
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


