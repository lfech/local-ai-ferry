"""Project defaults and INI overrides for the launcher."""

from configparser import ConfigParser, Error as INIError
from dataclasses import dataclass
from ipaddress import ip_address
import json
from pathlib import Path
import re


CONFIG_NAME = "config.ini"
DEFAULT_WAIT_TIMEOUT = 300
DEFAULT_COMPRESSION_LEVEL = 1
OPEN_TERMINAL_IMAGE = "ghcr.io/open-webui/open-terminal:0.17.20-slim"
TERMINAL_SECRET = Path("config/terminal-server.key")
LEGACY_TERMINAL_SECRET = Path("config/tool-server.key")
TERMINAL_KEY_PATTERN = re.compile(rb"[!-~]{32,512}(?:\r?\n)?")
TEST_FLAGS = (
    "run_docker_tests", "run_gpu_tests", "run_catalog_tests", "run_terminal_server_tests", "allow_legacy_terminal_bridge",
)
LEGACY_TEST_FLAGS = {
    "run_tool_server_tests": "run_terminal_server_tests",
    "allow_legacy_tool_bridge": "allow_legacy_terminal_bridge",
}


class ConfigError(ValueError):
    """The project configuration could not be read or validated."""


@dataclass(frozen=True)
class Settings:
    version: str = ""
    models_file: Path | None = None
    wait_timeout: int = DEFAULT_WAIT_TIMEOUT
    ollama_image: str = "ollama/ollama:latest"
    webui_image: str = "ghcr.io/open-webui/open-webui:main"
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
    terminal_server_enabled: bool = False
    terminal_server_include_in_backup: bool = False
    terminal_server_secret_file: Path = TERMINAL_SECRET
    terminal_server_apt_source_file: Path | None = None
    terminal_server_cpus: int = 2
    terminal_server_memory_mib: int = 2048
    terminal_server_max_workspaces: int = 4
    terminal_server_idle_seconds: int = 900
    terminal_server_command_seconds: int = 900
    terminal_server_volume_driver: str = "local"
    terminal_server_volume_options: str = "{}"
    run_docker_tests: bool = False
    run_gpu_tests: bool = False
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
            "LOAF_TERMINAL_VOLUME_DRIVER": self.terminal_server_volume_driver,
            "LOAF_TERMINAL_VOLUME_OPTIONS": self.terminal_server_volume_options,
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


def namespace(version: str) -> str:
    if not version:
        return ""
    name = f"{version}-local-inference"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", version) or len(name) > 128:
        raise ValueError("expected a lowercase Compose name within Docker's tag length limit (e.g. v1-2-3)")
    return name


def version_value(value: str) -> str:
    namespace(value)
    return value


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError("expected a positive integer")
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


def apt_source_file_value(value: str) -> Path | None:
    path = optional_path_value(value)
    if path is not None and path.suffix not in (".list", ".sources"):
        raise ValueError("expected a Debian APT file ending in .list or .sources")
    return path


SECTIONS = {
    "launcher": {"version": version_value, "models_file": optional_path_value, "wait_timeout": positive_int},
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
    },
    "terminal_server": {
        "enabled": boolean_value,
        "include_in_backup": boolean_value,
        "secret_file": path_value,
        "apt_source_file": apt_source_file_value,
        "base_image": image_value,
        "cpus": positive_int,
        "memory_mib": positive_int,
        "max_workspaces": positive_int,
        "idle_seconds": positive_int,
        "command_seconds": positive_int,
        "volume_driver": image_value,
        "volume_options": volume_options_value,
    },
    "tests": dict.fromkeys(TEST_FLAGS, boolean_value),
}


def load(*directories: Path) -> Settings:
    """Apply settings from each directory in order, starting with the built-ins."""
    values = {}
    for directory in dict.fromkeys(directories):
        values.update(_read_values(directory / CONFIG_NAME))
    return Settings(**values)


def read_terminal_secret(path: Path) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(f"Terminal server key file is missing: {path}")
    # Read only enough to detect an overlong key file, without exposing its contents.
    with path.open("rb") as source:
        key = source.read(515)
    if not TERMINAL_KEY_PATTERN.fullmatch(key):
        raise ConfigError(f"Invalid terminal server key in {path}: expected 32–512 printable ASCII characters without spaces.")
    return key


def terminal_secret_path(project: Path, configured: Path) -> Path:
    configured = configured.expanduser()
    path = project / configured
    if configured == TERMINAL_SECRET and not path.exists():
        legacy = project / LEGACY_TERMINAL_SECRET
        if legacy.is_file():
            return legacy.resolve()
    return path.resolve()


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
    aliases = {"tool_server": 0, "terminal-server": 1}
    # Load aliases first so current settings win within a file.
    for section in sorted(parser.sections(), key=lambda name: aliases.get(name, 2)):
        current = "terminal_server" if section in aliases else section
        if current not in SECTIONS:
            raise ConfigError(f"{path}: unknown section [{section}].")
        for key, value in parser[section].items():
            legacy_base = current == "terminal_server" and key == "base_image"
            if legacy_base and parser.has_option("images", "terminal_server_image"):
                continue
            current_key = LEGACY_TEST_FLAGS.get(key, key) if current == "tests" else key
            if current_key != key and current_key in parser[section]:
                continue
            try:
                convert = SECTIONS[current][current_key]
            except KeyError as exc:
                raise ConfigError(f"{path}: unknown setting [{section}] {key}.") from exc
            try:
                if any(character in value for character in ("\n", "\x00")):
                    raise ValueError("expected a single line of text")
                setting = f"terminal_server_{current_key}" if current == "terminal_server" else current_key
                if legacy_base:
                    setting = "terminal_server_image"
                values[setting] = convert(value)
            except ValueError as exc:
                raise ConfigError(f"{path}: [{section}] {key}: {exc}.") from exc
    return values
