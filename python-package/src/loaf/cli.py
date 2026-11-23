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
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
from typing import Iterator, Sequence
import zlib

from . import assets, cleanup, config, integrations, readiness, setup, transfer
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
MODEL_TRANSFER_MODES = ("export", "import")
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


def select_models_file(
    config_dir: Path, defaults_dir: Path, override: Path | None, configured: Path | None,
) -> Path:
    if override is not None:
        return override.absolute()
    name = configured or "models.ini"
    local = config_dir / name
    packaged = defaults_dir / name
    if local.exists() or local.is_symlink() or not packaged.is_file():
        return local
    return packaged


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
        description = "Start offline Ollama and import saved Modelfiles"
    steps += [stop, Step((profile,), startup + targets, f"{description} and wait up to {wait_timeout}s for readiness")]
    if start_terminal:
        steps.append(Step(("terminal",), startup + ("terminal-server",), "Start the terminal server"))
    if mode == "download":
        exec_args = ("exec", "-T", *targets)
        pulled = set()
        for model in models:
            if not export_definitions and model.source not in pulled:
                steps.append(Step((profile,), (*exec_args, "ollama", "pull", model.source), f"Download {model.source}"))
                pulled.add(model.source)
            if model.name:
                steps.append(Step(
                    (profile,), (*exec_args, "ollama", "cp", model.source, model.name),
                    f"Tag {model.source} as {model.name}",
                ))
            if export_definitions:
                name = model.name or model.source
                steps.append(Step((profile,), (*exec_args, "export-modelfiles", name), f"Export the Modelfile for {name}"))
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
        for path in self.files:
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
        if apt_source is not None and step is not None and step.arguments[0] == "build" and "terminal-server" in step.arguments:
            path = (self.project_dir / apt_source.expanduser()).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Terminal server APT source file is missing: {path}")
            digest = sha256()
            with path.open("rb") as source:
                while block := source.read(65536):
                    digest.update(block)
            variables.update(LOAF_TERMINAL_APT_SOURCE_FILE=str(path), LOAF_TERMINAL_APT_SOURCE_FORMAT=apt_source.suffix[1:],
                             LOAF_TERMINAL_APT_SOURCE_HASH=digest.hexdigest())
        return variables

    def terminal_secret_file(self) -> Path:
        return config.terminal_secret_path(self.project_dir, self.settings.terminal_server_secret_file)

    def environment(self, variables: dict[str, str]) -> dict[str, str]:
        # Supply the same settings to older Compose files and mask stale namespace values in .env.
        legacy = {f"OWO_{name.removeprefix('LOAF_')}": value
                  for name, value in variables.items() if name.startswith("LOAF_")}
        return {**os.environ, **legacy, **variables}

    def stop_legacy_terminal_server(self, env: dict[str, str]) -> None:
        command = [
            "docker", "ps", "--quiet", "--filter", f"label=com.docker.compose.project={self.project_name}",
            "--filter", "label=com.docker.compose.service=tool-server",
        ]
        running = subprocess.run(
            command, cwd=self.project_dir, check=True, env=env, stdout=subprocess.PIPE, text=True,
        ).stdout.split()
        if running:
            print("Stop the previous terminal server…", file=sys.stderr, flush=True)
            subprocess.run(
                ["docker", "stop", "--time", "45", *running], cwd=self.project_dir, check=True, env=env,
            )

    def trace(self, step: Step, variables: dict[str, str] | None = None) -> None:
        defaults = config.Settings().compose_variables()
        assignments = []
        values = self.variables(step) if variables is None else variables
        if not self.namespace and ("OWO_NAMESPACE" in os.environ or (self.project_dir / ".env").is_file()):
            values = {**values, "OWO_NAMESPACE": ""}
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
        if action == "stop" and "terminal" in step.profiles:
            self.stop_legacy_terminal_server(env)
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


GLOBAL_OPTIONS = ("--help", "--project-dir", "--verbose")
DEPLOYMENT_OPTIONS = ("--config-dir", "--version", "--dry-run")
COMPOSE_OPTIONS = (*DEPLOYMENT_OPTIONS, "--attach")
START_OPTIONS = (*COMPOSE_OPTIONS, "--wait-timeout")
TRANSFER_OPTIONS = (*DEPLOYMENT_OPTIONS, "--backup-dir", "--archive")
SETUP_OPTIONS = ("--config-dir", "--dry-run", "--archive", "--with-docker-deps")
SETUP_NOTES = "With the standalone executable, use --archive to select a backup folder."
COMMAND_HELP = {
    "setup": CommandHelp("Prepare a deployment or install loaf from source.", SETUP_OPTIONS, SETUP_NOTES),
    "install": CommandHelp("Alias for setup.", SETUP_OPTIONS, SETUP_NOTES),
    "binary": CommandHelp("Build the standalone executable at dist/loaf.", ("--dry-run",)),
    "clean": CommandHelp("Remove generated temporary files and the runtime cache.", ("--dry-run", "--full")),
    "link-docker-project": CommandHelp("Link the packaged Docker project for direct Compose use.", ("--dry-run",)),
    "build": CommandHelp("Build the local deployment images.", COMPOSE_OPTIONS),
    "upgrade": CommandHelp("Pull updated base images and rebuild local images.", COMPOSE_OPTIONS),
    "download": CommandHelp("Pull the selected Ollama models.", (*START_OPTIONS, "--models-file")),
    "online": CommandHelp("Start Ollama and WebUI with internet access.", (*START_OPTIONS, "--cpu")),
    "offline": CommandHelp("Start Ollama and WebUI for disconnected use.", (*START_OPTIONS, "--cpu")),
    "status": CommandHelp("Check deployment readiness and list containers.", (*COMPOSE_OPTIONS, "--cpu")),
    "stop": CommandHelp(
        "Stop deployment services and remove their containers.", (*COMPOSE_OPTIONS, "--remove-containers", "-n"),
    ),
    "export": CommandHelp("Save the Ollama model store to an archive.", TRANSFER_OPTIONS),
    "import": CommandHelp("Merge a model archive into the Ollama store.", (*TRANSFER_OPTIONS, "--delete")),
    "backup": CommandHelp(
        "Save the deployment in a portable backup folder.",
        (*TRANSFER_OPTIONS, "--verify", "--no-terminal-server", "--export", "--models-file", "--wait-timeout", "--attach"),
        "Use --archive with --verify. Use --models-file and --wait-timeout when\n"
        "exporting Modelfiles. Use --attach and --no-terminal-server when creating\n"
        "a backup.",
    ),
    "restore": CommandHelp(
        "Restore a full backup into new or empty Docker volumes.",
        (*TRANSFER_OPTIONS, "--import", "--wait-timeout", "--attach"),
        "Use --wait-timeout when starting Ollama to import saved Modelfiles.",
    ),
    "integration": CommandHelp("Print a host integration."),
    "test": CommandHelp("Run the checkout's verification suite.", ("--config-dir", "--badges")),
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
    add("--project-dir", type=Path, metavar="PATH", help="Use PATH as the project (default: current directory).")
    add("-v", "--verbose", action="store_true", help="Show external commands or individual test names.")
    add("--config-dir", type=Path, metavar="PATH", help="Use PATH for settings (default: project config/).")
    add("--version", metavar="VERSION", help="Select a deployment namespace. Use --version= to clear it.")
    add("--dry-run", action="store_true", help="Preview without running Docker or changing the project.")
    add("--models-file", type=Path, metavar="PATH", help="Select the model INI for this run.")
    add("--backup-dir", type=Path, metavar="PATH", help="Override the configured archive directory.")
    add("--archive", type=Path, metavar="PATH", help="Select an archive or backup folder.")
    add("--verify", action="store_true", help="Check an existing backup's hashes and image platforms without Docker.")
    add("--no-terminal-server", action="store_true", help="Skip the locally available Open Terminal image.")
    add("--delete", action="store_true", help="Delete destination model data absent from the archive.")
    add("--export", dest="export_models", action=argparse.BooleanOptionalAction,
        help="Enable or disable exporting selected Modelfiles from the cached models before backup.")
    add("--import", dest="import_models", action=argparse.BooleanOptionalAction,
        help="Enable or disable starting offline Ollama to apply saved Modelfiles after restore.")
    add("--remove-containers", action=argparse.BooleanOptionalAction,
        help="Remove stopped service containers by default, or keep them with --no-remove-containers.")
    add("-n", dest="remove_containers", action="store_false", default=None, help="Keep stopped service containers and logs.")
    add("--cpu", action="store_true", help="Use CPU inference instead of the default NVIDIA GPU.")
    add("--with-docker-deps", action="store_true",
        help="On Debian 13, cache Docker Engine, Compose and Buildx packages and install them as needed.")
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
    parser = argparse.ArgumentParser(
        prog=prog, usage=usage, description=description, epilog=notes, add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    if options:
        add_options(parser.add_argument_group("Options"), options)
    add_options(parser.add_argument_group("Global options"), GLOBAL_OPTIONS)
    parser.print_help()
    parser.exit()


def project_command(project: Path, config_dir: Path | None, version: str | None) -> list[str]:
    executable = os.environ.get("LOAF_RELEASE_EXECUTABLE")
    if executable and setup.bundled_release() is not None and transfer.standalone_executable(Path(executable)):
        command = [executable]
    else:
        command = [sys.executable, "-m", "loaf"]
    command.extend(("--project-dir", str(project)))
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


def build_executable(project: Path, script: Path, *, verbose: bool) -> int:
    command = ["bash", str(script), "--binary-only"]
    if verbose:
        print("+ " + display_command(command), flush=True)
    env = {**os.environ, "LOAF_BUILD_PYTHON": sys.executable}
    return subprocess.run(command, cwd=project, env=env, check=False).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loaf", usage=CLI_USAGE, add_help=False)
    parser.add_argument("mode", nargs="?", choices=COMMAND_HELP)
    parser.add_argument("integration", nargs="?", choices=INTEGRATION_HELP)
    add_options(parser)
    args = parser.parse_intermixed_args(argv)
    if args.mode != "integration" and args.integration is not None:
        parser.error("Use the integration name with loaf integration.")
    if args.help:
        show_help(args.mode, args.integration)
    if args.mode is None:
        parser.error("A command is required. See 'loaf --help'.")
    if args.mode == "integration" and args.integration is None:
        parser.error("integration requires a name: systemd, continue or tether.")
    continue_integration = args.integration == "continue"
    models_only = args.mode in MODEL_TRANSFER_MODES
    if args.remove_containers is not None and args.mode != "stop":
        parser.error("--remove-containers, --no-remove-containers and -n are only supported by stop.")
    remove_containers = args.remove_containers is not False
    if args.verify and args.mode != "backup":
        parser.error("--verify is only supported by backup.")
    if args.no_terminal_server and (args.mode != "backup" or args.verify):
        parser.error("--no-terminal-server is only supported when creating a backup.")
    if args.delete and args.mode != "import":
        parser.error("--delete is only supported by model import.")
    if args.cpu and args.mode not in ("online", "offline", "status"):
        parser.error("--cpu is only supported by online, offline and status.")
    if args.with_docker_deps and args.mode not in INSTALL_MODES:
        parser.error("--with-docker-deps is only supported by setup and install.")
    if args.badges and args.mode != "test":
        parser.error("--badges is only supported by test.")
    if args.full and args.mode != "clean":
        parser.error("--full is only supported by clean.")
    if args.backup_dir is not None and args.mode not in TRANSFER_MODES and not models_only:
        parser.error("--backup-dir is only supported by backup, restore, import and export.")
    if args.attach and (args.verify or args.mode not in (*MODES, *TRANSFER_MODES)):
        parser.error("--attach is available for Compose and full transfer commands.")
    project_dir = (args.project_dir or Path.cwd()).resolve()
    if args.mode in (LINK_MODE, "binary", "clean"):
        if any(value is not None for value in (
            args.config_dir, args.version, args.models_file, args.backup_dir, args.archive,
            args.export_models, args.import_models, args.wait_timeout,
        )):
            parser.error(f"Unexpected option for {args.mode}. See 'loaf {args.mode} --help'.")
        try:
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
        except (OSError, AlreadyRunningError, subprocess.CalledProcessError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    compose_file = assets.compose_path(project_dir)
    data_dir = assets.DATA_DIR if compose_file.parent == project_dir else compose_file.parent
    defaults_dir = data_dir / "config"
    terminal_available = (compose_file.parent / "docker/terminal-server/Dockerfile").is_file()
    config_dir = args.config_dir.resolve() if args.config_dir else project_dir / "config"
    if args.config_dir is not None and not config_dir.is_dir():
        parser.error(f"Configuration directory does not exist: {config_dir}.")
    standalone_setup = args.mode in INSTALL_MODES and setup.bundled_release() is not None
    if args.backup_dir is not None and args.archive is not None:
        parser.error("Use either --backup-dir or --archive.")
    if args.archive is not None and (
        args.mode != "restore" and not (models_only or args.verify or standalone_setup)
    ):
        parser.error("--archive is for standalone setup, restore, model transfers or backup --verify.")
    if args.mode == "import" and args.archive is not None and transfer.is_bundle(args.archive):
        parser.error("Use loaf restore for a full backup folder.")
    bundle = None
    try:
        settings = config.load(defaults_dir, config_dir)
        version = settings.version if args.version is None else args.version
        namespace = config.namespace(version)
        if args.mode == "restore" or args.verify or standalone_setup:
            selection = (args.archive or args.backup_dir)
            selection = selection.resolve() if selection is not None else None
            directory = transfer.find_bundle(project_dir, selection)
            if directory is None and not (standalone_setup or args.archive):
                root = selection or project_dir / settings.backup_dir
                candidate = transfer.latest_backup(root, namespace=namespace)
                if candidate.is_dir():
                    directory = candidate
            if directory is not None:
                bundle = transfer.read_bundle(directory, verify=not args.dry_run)
                if args.archive is not None and selection not in (directory, bundle.archive):
                    raise transfer.TransferError(f"The backup manifest selects {bundle.archive}.")
                if args.version is not None and args.version != bundle.version:
                    raise transfer.TransferError(f"Backup version is {bundle.version or 'unversioned'}. Select the matching --version.")
                version = bundle.version
                namespace = config.namespace(version)
            elif standalone_setup and args.archive is not None:
                raise transfer.TransferError("Standalone setup requires a backup folder with backup.json.")
    except (OSError, transfer.TransferError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except config.ConfigError as exc:
        parser.error(str(exc))
    except ValueError as exc:
        parser.error(f"--version: {exc}.")
    if args.export_models is not None and (args.mode != "backup" or args.verify):
        parser.error("--export/--no-export are only supported when creating a backup.")
    if args.import_models is not None and args.mode != "restore":
        parser.error("--import/--no-import are only supported by restore.")
    export_models = args.mode == "backup" and not args.verify and (
        settings.export_models if args.export_models is None else args.export_models
    )
    import_models = args.mode == "restore" and (
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
            parser.error("test supports --project-dir, --config-dir, -v and --badges. See 'loaf test --help'.")
        tests_dir = project_dir / TESTS_DIR
        if not tests_dir.is_dir():
            parser.error(f"No tests directory at {tests_dir}.")
        # A child isolates suite imports. Require a nonempty suite on every
        # supported Python version.
        command = [sys.executable, "-c", TEST_RUNNER, str(tests_dir), "2" if args.verbose else "0"]
        if args.badges:
            command.append(str(Path(__file__).resolve().parents[1]))
        env = dict(os.environ)
        for old, current in config.LEGACY_TEST_FLAGS.items():
            if old.upper() in env:
                env.setdefault(current.upper(), env[old.upper()])
        for flag in config.TEST_FLAGS:
            env.setdefault(flag.upper(), "1" if getattr(settings, flag) else "0")
        try:
            return subprocess.run(command, cwd=project_dir, check=False, env=env).returncode
        except OSError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    if args.integration == "tether":
        print(integrations.TETHER_INSTRUCTIONS, end="")
        return 0
    if not (continue_integration or args.verify or standalone_setup or compose_file.is_file()):
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
        if args.integration == "systemd":
            if sys.platform != "linux":
                parser.error("integration systemd requires Linux.")
            launcher = project_command(project_dir, override_dir, version)
            print(integrations.systemd_unit(project_dir, launcher, os.getuid()), end="")
            return 0
        models = []
        if workflow_mode == "download" or continue_integration:
            models_file = select_models_file(config_dir, defaults_dir, args.models_file, settings.models_file)
            models = read_models(models_file)
        if continue_integration:
            print(integrations.continue_config(settings.webui_url(), ((m.name, m.source) for m in models)), end="")
            return 0
        if workflow_mode in MODES:
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
            if standalone_setup:
                if args.dry_run:
                    print(f"Prepare default configuration and link the packaged Docker project in {project_dir}.")
                    if args.with_docker_deps:
                        print("Prepare Docker host packages" + (f" from {bundle.archive}." if bundle else "."))
                else:
                    with project_lock(lock_path):
                        if args.with_docker_deps:
                            if bundle is not None:
                                transfer.recover_installers(bundle, project_dir)
                            setup.prepare_docker(project_dir, allow_downloads=bundle is None)
                            if bundle is None or list((project_dir / setup.TOOLKIT_DIR).glob("*.deb")):
                                setup.install_toolkit(project_dir, allow_downloads=bundle is None)
                        setup.setup_standalone(project_dir)
                return 0
            if args.dry_run:
                if args.with_docker_deps:
                    print("Cache and, if needed, install Debian Docker Engine, Compose and Buildx packages.")
                print(f"Prepare {project_dir / '.venv'}, cache Python wheels and prepare the Debian NVIDIA toolkit.")
            else:
                with project_lock(lock_path):
                    try:
                        setup.install(project_dir, with_docker_deps=args.with_docker_deps)
                    except (OSError, setup.SetupError, subprocess.CalledProcessError):
                        print("See docs/debian.md for Debian host setup.", file=sys.stderr)
                        raise
            return 0
        compose = Compose(project_dir, files=compose_files, namespace=namespace, settings=settings, verbose=args.verbose)
        if settings.terminal_server_enabled and args.mode in ("online", "offline") and not args.dry_run:
            config.read_terminal_secret(compose.terminal_secret_file())
        if args.mode in TRANSFER_MODES or models_only:
            backup = args.mode in ("backup", "export") and not args.verify
            with (nullcontext() if args.dry_run or args.verify else project_lock(lock_path)):
                if bundle is not None:
                    archive = bundle.archive
                elif args.archive is not None:
                    archive = args.archive.resolve()
                else:
                    directory = (args.backup_dir or project_dir / settings.backup_dir).resolve()
                    select_archive = transfer.backup_path if backup else transfer.latest_backup
                    archive = select_archive(directory, namespace=namespace, models_only=models_only)
                if args.verify:
                    if args.dry_run:
                        print(f"Verify payload hashes and report image platforms from {archive}")
                    else:
                        transfer.verify_backup(archive, namespace)
                    return 0
                if models_only:
                    def stop_downloader():
                        compose.execute(VALIDATE)
                        compose.execute(STOP_DOWNLOADER)

                    if args.dry_run:
                        execute_plan(compose, [VALIDATE, STOP_DOWNLOADER], lock_path=lock_path, dry_run=True)
                        if backup:
                            print(f"Save Ollama models to {archive}")
                        elif args.delete:
                            print(f"Replace Ollama models from {archive} and delete destination-only model data")
                        else:
                            print(f"Merge Ollama models from {archive}")
                    elif backup:
                        transfer.export_models(
                            project_dir, archive, namespace, settings.compression_level, stop_downloader,
                        )
                        print(f"Ollama models saved to {archive}")
                    else:
                        transfer.import_models(project_dir, archive, namespace, stop_downloader, delete=args.delete)
                        print(f"Ollama models imported from {archive}")
                    return 0
                if backup:
                    steps.append(STOP_WITH_TERMINAL if terminal_available else STOP_ALL)
                script = binary_builder(project_dir) if backup else None
                if args.dry_run:
                    if backup:
                        if script is not None:
                            print(f"Build the standalone executable at {project_dir / 'dist/loaf'}")
                        print("Export Python wheels")
                        print("Save images and project data with the standalone loaf executable")
                        print("Skip the Open Terminal image" if args.no_terminal_server else
                              "Include the Open Terminal image if available locally")
                    else:
                        print(f"Load images and restore empty volumes from {archive}")
                        if bundle is not None:
                            print(f"Restore saved settings and Modelfiles into {config_dir}")
                    execute_plan(compose, steps, lock_path=lock_path, dry_run=True)
                    if backup:
                        print(f"Archive service volumes to {archive}")
                        if settings.terminal_server_include_in_backup:
                            print("Include terminal workspace volumes and bearer key")
                        print("Check saved Modelfiles against the Ollama model store")
                elif backup:
                    executable = None
                    if script is not None:
                        result = build_executable(project_dir, script, verbose=args.verbose)
                        if result:
                            return result
                        executable = project_dir / "dist/loaf"

                    def prepare_volumes():
                        for step in steps:
                            compose.execute(step)

                    transfer.backup_bundle(
                        project_dir, archive, prepare_volumes,
                        version=version, executable=executable, compression_level=settings.compression_level,
                        exclude_project_file=compose.terminal_secret_file(),
                        include_terminal=settings.terminal_server_include_in_backup,
                        include_terminal_image=not args.no_terminal_server,
                        config_dir=config_dir,
                    )
                    print(f"Backup saved to {archive}", flush=True)
                else:
                    recovered = transfer.restore(
                        project_dir, archive, namespace=namespace, terminal_key=compose.terminal_secret_file(),
                        workspace_driver=settings.terminal_server_volume_driver,
                        workspace_options=settings.terminal_server_volume_options,
                        configuration=(defaults_dir, config_dir, bundle.version) if bundle is not None else None,
                    )
                    if recovered is not None:
                        compose = Compose(project_dir, files=compose_files, namespace=namespace, settings=recovered,
                                          verbose=args.verbose)
                        import_models = recovered.import_models if args.import_models is None else args.import_models
                        workflow_mode = "offline" if import_models else "restore"
                        steps = plan("offline", wait_timeout=args.wait_timeout or recovered.wait_timeout,
                                     import_definitions=True) if import_models else []
                    print(f"Restored from {archive}", flush=True)
                    for step in steps:
                        compose.execute(step)
                    if import_models:
                        print("Modelfiles imported. GPU Ollama is ready. Open WebUI is stopped.", flush=True)
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


