"""Project defaults and INI overrides for the launcher."""

from configparser import ConfigParser, Error as INIError
from dataclasses import dataclass
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re


CONFIG_NAME = "config.ini"
# The default deployment's namespace. [launcher] namespace = NAME selects the namespace loaf-NAME.
DEPLOYMENT = "loaf"
# The system layer between the packaged defaults and a project's config/.
SYSTEM_CONFIG_DIR = Path("/etc/loaf")
DEFAULT_WAIT_TIMEOUT = 300
DEFAULT_COMPRESSION_LEVEL = 4
# The archive format this release writes; [transfer] backup_version_min can raise the oldest accepted one.
# Format 3 stores the terminal type as one user volume. Releases before v1.0.0 read only their own format.
ARCHIVE_VERSION = 3
OPEN_TERMINAL_IMAGE = "ghcr.io/open-webui/open-terminal:0.17.20-slim"
TERMINAL_SECRET = Path("config/terminal-server.key")
# Open WebUI's terminal proxy gives a request 300 seconds; the command wait leaves time for the rest.
COMMAND_WAIT_MAX = 240
TERMINAL_KEY_PATTERN = re.compile(rb"[!-~]{32,512}(?:\r?\n)?")
TEST_FLAGS = (
    "run_docker_tests", "run_gpu_tests", "run_cpu_tests", "run_catalog_tests", "run_terminal_server_tests",
    "allow_legacy_terminal_bridge",
)


class ConfigError(ValueError):
    """The project configuration could not be read or validated."""


@dataclass(frozen=True)
class Settings:
    namespace_name: str = ""
    models_file: Path | None = None
    wait_timeout: int = DEFAULT_WAIT_TIMEOUT
    # Whether Compose reads a .env file in the project directory, whose variables override settings.
    docker_env_enable: bool = False
    ollama_image: str = "loaf-ollama-cuda13"
    webui_image: str = "ghcr.io/open-webui/open-webui:v0.13.4-slim"
    terminal_server_image: str = OPEN_TERMINAL_IMAGE
    context_length: int = 16384
    startup_timeout: int = 60
    cuda_visible_devices: str = ""
    bind_address: str = "127.0.0.1"
    port: int = 3000
    auth: bool = True
    ollama_cloud: bool = True
    webui_downloads: bool = True
    backup_dir: Path = Path("backup")
    export_models: bool = False
    import_models: bool = False
    compression_level: int = DEFAULT_COMPRESSION_LEVEL
    backup_version_min: int = ARCHIVE_VERSION
    terminal_server_enabled: bool = False
    terminal_server_include_in_backup: bool = True
    terminal_server_secret_file: Path = TERMINAL_SECRET
    terminal_server_apt_source_file: Path | None = None
    terminal_server_apt_repository_dir: Path | None = None
    # isolated: chat containers reach only the provisioner; connected: also every address the host reaches.
    terminal_server_network: str = "isolated"
    # Open WebUI accounts that may use the preset terminal connection: admins, or users for every account.
    terminal_server_webui_access: str = "admins"
    terminal_server_cpus: int = 2
    terminal_server_memory_mib: int = 2048
    terminal_server_max_workspaces: int = 4
    terminal_server_idle_seconds: int = 900
    terminal_server_command_seconds: int = 900
    # Seconds a model's command may run before run_command returns without its result; 0 returns at once.
    terminal_server_command_wait_seconds: int = 120
    terminal_server_volume_driver: str = "local"
    terminal_server_volume_options: str = "{}"
    run_docker_tests: bool = False
    run_gpu_tests: bool = False
    run_cpu_tests: bool = False
    run_catalog_tests: bool = False
    run_terminal_server_tests: bool = False
    allow_legacy_terminal_bridge: bool = False

    def compose_variables(self) -> dict[str, str]:
        return {
            "LOAF_INI_OLLAMA_IMAGE": self.ollama_image,
            "LOAF_INI_WEBUI_IMAGE": self.webui_image,
            "LOAF_INI_CONTEXT_LENGTH": str(self.context_length),
            "LOAF_INI_STARTUP_TIMEOUT": str(self.startup_timeout),
            "LOAF_WEBUI_BIND_ADDRESS": self.bind_address,
            "LOAF_WEBUI_PORT": str(self.port),
            "LOAF_WEBUI_AUTH": str(self.auth).lower(),
            "LOAF_ONLINE_NO_CLOUD": str(not self.ollama_cloud).lower(),
            "LOAF_ONLINE_OFFLINE_MODE": str(not self.webui_downloads).lower(),
            "LOAF_ONLINE_HF_HUB_OFFLINE": "0" if self.webui_downloads else "1",
            "LOAF_TERMINAL_BASE_IMAGE": self.terminal_server_image,
            "LOAF_TERMINAL_CPUS": str(self.terminal_server_cpus),
            "LOAF_TERMINAL_MEMORY_MIB": str(self.terminal_server_memory_mib),
            "LOAF_TERMINAL_MAX_WORKSPACES": str(self.terminal_server_max_workspaces),
            "LOAF_TERMINAL_IDLE_SECONDS": str(self.terminal_server_idle_seconds),
            "LOAF_TERMINAL_COMMAND_SECONDS": str(self.terminal_server_command_seconds),
            "LOAF_TERMINAL_COMMAND_WAIT_SECONDS": str(self.terminal_server_command_wait_seconds),
            "LOAF_TERMINAL_VOLUME_DRIVER": self.terminal_server_volume_driver,
            "LOAF_TERMINAL_VOLUME_OPTIONS": self.terminal_server_volume_options,
            "LOAF_TERMINAL_NETWORK": self.terminal_server_network,
            "LOAF_TERMINAL_WEBUI_ACCESS": self.terminal_server_webui_access,
        }

    def webui_url(self) -> str:
        address = ip_address(self.bind_address)
        if self.bind_address in ("127.0.0.1", "0.0.0.0"):
            host = "localhost"
        elif address.version == 6:
            host = "[::1]" if address.is_unspecified else f"[{address}]"
        else:
            host = str(address)
        return f"http://{host}:{self.port}"


def namespace(name: str) -> str:
    """The deployment's Compose project, loaf-NAME, which starts the names of its containers, networks and volumes."""
    if not name:
        return DEPLOYMENT
    full = f"{DEPLOYMENT}-{name}"
    # The name tags the images, so latest would share the default deployment's images.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) or name == "latest" or len(full) > 128:
        raise ValueError("expected a lowercase Compose name other than latest within Docker's tag length limit "
                         "(e.g. v1-2-3)")
    return full


def namespace_name(namespace: str) -> str:
    """The NAME in a namespace loaf-NAME; empty for the default deployment."""
    return "" if namespace == DEPLOYMENT else namespace.removeprefix(f"{DEPLOYMENT}-")


def namespace_value(value: str) -> str:
    namespace(value)
    return value


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError("expected a positive integer")
    return number


def wait_timeout_value(value: str) -> int:
    number = int(value)
    if number < 0:
        raise ValueError("expected 0 to wait without a limit or a positive number of seconds")
    return number


def command_wait_value(value: str) -> int:
    number = int(value)
    if not 0 <= number <= COMMAND_WAIT_MAX:
        raise ValueError(f"expected 0 to return at once or at most {COMMAND_WAIT_MAX} seconds")
    return number


def port_value(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise ValueError("expected a port from 1 to 65535")
    return number


def compression_value(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 9:
        raise ValueError("expected a compression level from 0 to 9")
    return number


def archive_version_value(value: str) -> int:
    number = int(value)
    # Format 3 changed the terminal type; this release reads nothing older or newer than it writes.
    if number != ARCHIVE_VERSION:
        raise ValueError(f"expected archive format {ARCHIVE_VERSION}")
    return number


def boolean_value(value: str) -> bool:
    try:
        return ConfigParser.BOOLEAN_STATES[value.lower()]
    except KeyError as exc:
        raise ValueError("expected true or false") from exc


def path_value(value: str) -> Path:
    if not value:
        raise ValueError("expected a path")
    return Path(value)


def optional_path_value(value: str) -> Path | None:
    return Path(value) if value else None


def system_config_value(value: str) -> Path | None:
    if value and not Path(value).is_absolute():
        raise ValueError("expected an absolute directory, or blank to skip the system configuration")
    return Path(value) if value else None


def image_value(value: str) -> str:
    if not value or any(character.isspace() for character in value):
        raise ValueError("expected a nonempty image reference without spaces")
    return value


def address_value(value: str) -> str:
    try:
        address = ip_address(value)
    except ValueError as exc:
        raise ValueError("expected an IPv4 or IPv6 address") from exc
    if getattr(address, "scope_id", None):
        raise ValueError("expected an IP address without an interface scope")
    return str(address)


def volume_options_value(value: str) -> str:
    options = json.loads(value)
    if not isinstance(options, dict) or any(not isinstance(item, str) for item in options.values()):
        raise ValueError("expected a JSON object with string values")
    return json.dumps(options)


def terminal_webui_access_value(value: str) -> str:
    if value not in ("admins", "users"):
        raise ValueError("expected admins or users")
    return value


def terminal_network_value(value: str) -> str:
    if value not in ("isolated", "connected"):
        raise ValueError("expected isolated or connected")
    return value


def apt_source_file_value(value: str) -> Path | None:
    path = optional_path_value(value)
    if path is not None and path.suffix not in (".list", ".sources"):
        raise ValueError("expected a Debian APT file ending in .list or .sources")
    return path


SECTIONS = {
    "launcher": {
        "namespace": namespace_value, "models_file": optional_path_value, "wait_timeout": wait_timeout_value,
        "system_config": system_config_value, "docker_env_enable": boolean_value,
    },
    "images": {
        "ollama_image": image_value, "webui_image": image_value, "terminal_server_image": image_value,
    },
    "ollama": {"context_length": positive_int, "startup_timeout": positive_int, "cuda_visible_devices": str},
    "webui": {"bind_address": address_value, "port": port_value, "auth": boolean_value},
    "online": {"ollama_cloud": boolean_value, "webui_downloads": boolean_value},
    "transfer": {
        "backup_dir": path_value,
        "export_models": boolean_value,
        "import_models": boolean_value,
        "compression_level": compression_value,
        "backup_version_min": archive_version_value,
    },
    "terminal_server": {
        "enabled": boolean_value,
        "include_in_backup": boolean_value,
        "secret_file": path_value,
        "apt_source_file": apt_source_file_value,
        "apt_repository_dir": optional_path_value,
        "network": terminal_network_value,
        "webui_access": terminal_webui_access_value,
        "cpus": positive_int,
        "memory_mib": positive_int,
        "max_workspaces": positive_int,
        "idle_seconds": positive_int,
        "command_seconds": positive_int,
        "command_wait_seconds": command_wait_value,
        "volume_driver": image_value,
        "volume_options": volume_options_value,
    },
    "tests": dict.fromkeys(TEST_FLAGS, boolean_value),
}
# Settings that were renamed, with their current names.
RENAMED = {("launcher", "version"): "namespace"}


SECTION_LINE = re.compile(r"\s*\[(?P<name>[^]]+)\]\s*")
KEY_LINE = re.compile(r"(?P<comment>[#;]\s*)?(?P<key>[A-Za-z0-9_.-]+)\s*[=:]")


def edit_text(text: str, values: dict[tuple[str, str], str]) -> str:
    """Set settings in config.ini text by changing single lines; comments and all other lines stay.

    A value replaces the key's line in its section, or else the commented default (# key = ...), or else
    follows the section header; a missing section is appended. Lines of renamed settings get the current name.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    def entries():
        section = None
        for index, line in enumerate(lines):
            if header := SECTION_LINE.fullmatch(line.rstrip("\r\n")):
                section = header["name"].strip()
            elif match := KEY_LINE.match(line):
                yield index, section, match["key"].lower(), bool(match["comment"])

    for (section, old), new in RENAMED.items():
        for index, found, key, commented in list(entries()):
            if (found, key, commented) == (section, old, False):
                lines[index] = new + lines[index][len(old):]
    for (section, key), value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"[{section}] {key} must be a single line.")
        matches = [(index, commented) for index, found, name, commented in entries() if (found, name) == (section, key)]
        target = next((index for index, commented in sorted(matches, key=lambda match: match[1])), None)
        if target is not None:
            ending = lines[target][len(lines[target].rstrip("\r\n")):]
            lines[target] = f"{key} = {value}{ending}"
            continue
        header = next((index for index, line in enumerate(lines)
                       if (match := SECTION_LINE.fullmatch(line.rstrip("\r\n"))) and match["name"].strip() == section),
                      None)
        if header is None:
            lines += [*(["\n"] if lines and lines[-1].strip() else []), f"[{section}]\n", f"{key} = {value}\n"]
        else:
            lines.insert(header + 1, f"{key} = {value}\n")
    return "".join(lines)


def load(*directories: Path) -> Settings:
    """Apply settings from each directory in order, starting with the built-ins."""
    values = {}
    for directory in dict.fromkeys(directories):
        values.update(_read_values(directory / CONFIG_NAME))
    # system_config selects a layer and is not a deployment setting.
    values.pop("system_config", None)
    return Settings(**values)


def system_directory() -> Path | None:
    """The default system configuration. Tests use LOAF_SYSTEM_CONFIG_DIR to replace it."""
    configured = os.environ.get("LOAF_SYSTEM_CONFIG_DIR")
    if configured is None:
        return SYSTEM_CONFIG_DIR
    return Path(configured) if configured else None


def layers(defaults: Path, local: Path) -> tuple[Path, ...]:
    """Return the packaged defaults, the system configuration and the project's configuration.

    Later directories override earlier ones. Only the project's file can move or skip the system layer.
    """
    system = system_directory()
    values = _read_values(local / CONFIG_NAME)
    if "system_config" in values:
        system = values["system_config"]
    if system is not None and "system_config" in _read_values(system / CONFIG_NAME):
        raise ConfigError(f"{system / CONFIG_NAME}: set [launcher] system_config only in a project's config.ini.")
    return tuple(dict.fromkeys(directory for directory in (defaults, system, local) if directory is not None))


def shared_system_deployment(layer_directories: tuple[Path, ...], name: str) -> Path | None:
    """Return the system configuration when this project uses the same Docker namespace as the system deployment."""
    if len(layer_directories) < 3 or not (layer_directories[1] / CONFIG_NAME).is_file():
        return None
    system = load(*layer_directories[:2])
    return layer_directories[1] if namespace(system.namespace_name) == namespace(name) else None


def read_terminal_secret(path: Path) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(f"Terminal server key file is missing: {path}. Create it with 'loaf setup "
                                "terminal-server-key'. Run in a backup folder, that takes the backup's key.")
    # Read only enough to detect an overlong key file, without exposing its contents.
    with path.open("rb") as source:
        key = source.read(515)
    if not TERMINAL_KEY_PATTERN.fullmatch(key):
        raise ConfigError(f"Invalid terminal server key in {path}: expected 32–512 printable ASCII characters without spaces.")
    return key


def terminal_secret_path(project: Path, configured: Path) -> Path:
    return (project / configured.expanduser()).resolve()


def _read_values(path: Path) -> dict:
    parser = ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8-sig") as source:
            parser.read_file(source)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, INIError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if parser.defaults():
        raise ConfigError(f"{path}: use the named sections instead of [DEFAULT].")
    values = {}
    for section in parser.sections():
        if section not in SECTIONS:
            raise ConfigError(f"{path}: unknown section [{section}].")
        for key, value in parser[section].items():
            try:
                convert = SECTIONS[section][key]
            except KeyError as exc:
                renamed = RENAMED.get((section, key))
                hint = f" It is now [{section}] {renamed}." if renamed else ""
                raise ConfigError(f"{path}: unknown setting [{section}] {key}.{hint}") from exc
            try:
                if any(character in value for character in ("\n", "\x00")):
                    raise ValueError("expected a single line of text")
                setting = (f"terminal_server_{key}" if section == "terminal_server"
                           else "namespace_name" if (section, key) == ("launcher", "namespace") else key)
                values[setting] = convert(value)
            except ValueError as exc:
                raise ConfigError(f"{path}: [{section}] {key}: {exc}.") from exc
    return values
