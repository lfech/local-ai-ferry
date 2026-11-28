"""Inspect Debian APT sources before passing them to a build or chat container."""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from .config import ConfigError


REPOSITORY = PurePosixPath("/run/loaf-apt/repository")
HOST = "host.docker.internal"


@dataclass(frozen=True)
class Source:
    path: Path
    suffix: str
    text: str
    targets: tuple[tuple[str, str, str, str], ...]

    def covers(self, suite: str, component: str = "") -> bool:
        return any(kind == "Packages" and is_repository(uri) and release == suite and
                   section == (component or "$(COMPONENT)") for kind, uri, release, section in self.targets)

    def check_network(self, network: str, *, repository: bool) -> None:
        if network in ("internet", "local"):
            return
        for _, uri, _, _ in self.targets:
            url = urlsplit(uri)
            path = PurePosixPath(unquote(url.path))
            if (repository and url.scheme == "file" and not url.netloc and path.is_relative_to(REPOSITORY)
                    and ".." not in path.parts):
                continue
            if network == "host" and url.scheme in ("http", "https") and url.hostname == HOST:
                continue
            allowed = f"file:{REPOSITORY} or HTTP(S) at {HOST}" if network == "host" else f"file:{REPOSITORY}"
            raise ConfigError(f"Terminal server APT source file {self.path} with network = {network} requires "
                              f"sources at {allowed}. Set apt_repository_dir for a file source.")


def is_repository(uri: str) -> bool:
    url = urlsplit(uri)
    return url.scheme == "file" and not url.netloc and PurePosixPath(unquote(url.path)) == REPOSITORY


def read_source(path: Path, suffix: str) -> Source:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Terminal server APT source file {path} must be UTF-8.") from exc
    line = next((line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")), "")
    deb822 = re.match(r"[A-Za-z][A-Za-z0-9-]*:", line) is not None
    if line and deb822 != (suffix == ".sources"):
        format_hint = "deb822 stanzas. Use .sources." if deb822 else "one-line entries. Use .list."
        raise ConfigError(f"Terminal server APT source file {path} holds {format_hint}")
    targets = indexes({f"source{suffix}": text}, path, "has invalid Debian APT syntax")
    return Source(path, suffix, text, targets)


def check_combination(source: Source, generated: str) -> None:
    indexes({f"source{source.suffix}": source.text, "repository.list": generated}, source.path,
            "conflicts with the generated apt_repository_dir source")


def indexes(files: dict[str, str], path: Path, problem: str) -> tuple[tuple[str, str, str, str], ...]:
    # APT parses a copy named for the configured format, including sources that resolve through a symlink.
    # indextargets with no release info calculates targets without downloading indexes or updating system lists.
    with TemporaryDirectory(prefix="loaf-apt-") as folder:
        directory = Path(folder)
        parts = directory / "sources"
        parts.mkdir()
        for name, text in files.items():
            (parts / name).write_bytes(text.encode("utf-8"))
        config = directory / "apt.conf"
        config.write_text('Dir::Etc::parts "/dev/null";\nDir::Etc::main "/dev/null";\n', encoding="ascii")
        try:
            result = subprocess.run([
                "apt-get", "-o", "Debug::NoLocking=true", "-o", "Dir::Etc::SourceList=/dev/null",
                "-o", f"Dir::Etc::SourceParts={parts}", "-o", f"Dir::State::lists={directory / 'lists'}",
                "indextargets", "--no-release-info", "--format", "$(IDENTIFIER)\t$(REPO_URI)\t$(RELEASE)\t$(COMPONENT)",
            ], env={**os.environ, "APT_CONFIG": str(config), "LC_ALL": "C"}, capture_output=True, text=True,
               encoding="utf-8", errors="replace", check=False, timeout=10)
        except FileNotFoundError as exc:
            raise ConfigError(f"Install apt-get on the launcher host to check {path}.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ConfigError(f"APT syntax check timed out for {path}.") from exc
    if result.returncode:
        raise ConfigError(f"Terminal server APT source file {path} {problem}.")
    return tuple(tuple(line.split("\t")) for line in result.stdout.splitlines())
