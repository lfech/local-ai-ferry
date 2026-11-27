"""Transfer deployment data in one archive whose first member lists its typed contents."""

from __future__ import annotations

from configparser import ConfigParser
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from tempfile import NamedTemporaryFile, TemporaryDirectory, TemporaryFile
from typing import Callable

from . import assets, config, locks
from .progress import Progress, TrackedStream
from .setup import (
    INSTALL_CACHE, PYTHON_PACKAGE_DIR, PYTHON_WHEEL_DIR, SYSTEM_GROUP, TEST_WHEEL_RECORD, TOOLKIT_DIR,
    WHEEL_CACHE_RECORD, docker_installer_files, file_digest, prepare_python_wheels, protect_terminal_key, toolkit_files,
)


ARCHIVE_FORMAT = "loaf-archive"
ARCHIVE_VERSION = config.ARCHIVE_VERSION
MANIFEST = "loaf.json"
MANIFEST_LIMIT = 1024 * 1024
BUNDLE_NAME = "local-ai-ferry"
CHECKSUMS = "SHA256SUMS"
LAUNCHER = "loaf"
EXECUTABLE_HEADER = b"#!/bin/bash\n# loaf standalone backup launcher v1\n"
# Archive types in backup and restore order. Each type has one destination.
TYPES = ("source", "config", "installers", "images", "models", "webui", "chats")
ALIASES = {"project": ("config", "installers"), "volumes": ("models", "webui", "chats")}
# Types without namespaced Docker resources or settings can restore into another deployment version.
PORTABLE_TYPES = ("source", "installers", "models")
# Types whose capture requires stopped services. Models use a consistent snapshot instead.
SERVICE_TYPES = ("webui", "chats")
CHAT_VOLUMES = "chats/volumes.tar"
CHAT_IMAGES = "chats/images.tar"
CHAT_KEY = "chats/terminal-server.key"
MEMBER_NAMES = frozenset({*(f"{kind}.tar" for kind in TYPES), CHAT_VOLUMES, CHAT_IMAGES, CHAT_KEY})
CHECKOUT_RESERVED_PATHS = {CHECKSUMS, "backup", "dist", "docker-project", LAUNCHER, ".run.lock"}
CONFIG_SUFFIXES = {".ini", ".model", ".list", ".sources"}
TERMINAL_KEY_STAGING = ".loaf-terminal-key-"
IMAGES = ("local/ollama-online:latest", "local/ollama-offline:latest", "local/open-webui:latest")
VOLUMES = {"ollama": "ollama", "open-webui": "webui"}
# Match the ownership labels set by the terminal provisioner.
WORKSPACE_PROJECT_LABEL = "loaf.tool-server.project"
WORKSPACE_OWNER_LABEL = "loaf.tool-server.workspace"
CHAT_PROJECT_LABEL = "loaf.terminal.chat-project"
CHAT_ID_LABEL = "loaf.terminal.chat-id"
CHAT_OWNER_LABEL = "loaf.terminal.chat-owner"
CHAT_IMAGE_PROJECT_LABEL = "loaf.terminal.apt-project"
CHAT_IMAGE_TEMPORARY_LABEL = "loaf.terminal.apt-temporary"
CHAT_IMAGE_REPOSITORY = "local/loaf-terminal-chat"
# Temporary chats keep their folders only while the provisioner runs.
UNSAVED_CHAT_PREFIXES = ("temporary:", "local:", "channel:")
COPY_BUFFER = 1024 * 1024
MANIFEST_LIST_TYPE = "application/vnd.ollama.manifest.list.v2+json"

# Check every mount before extracting. On a failed extract,
# clear only the mounts that this restore found empty.
RESTORE_VOLUMES = """\
set -eu
for directory do
    entry=$(find "$directory" -mindepth 1 -print -quit)
    if [ -n "$entry" ]; then
        echo "Restore requires empty volumes. $directory already contains data." >&2
        exit 1
    fi
done
trap 'find "$@" -mindepth 1 -delete' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
tar -xpf - -C /transfer
trap - EXIT
"""

SHOW_SAVED_MODELFILES = """\
set -euo pipefail
export HOME=/tmp OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS=/ollama/models
export OLLAMA_NO_CLOUD=1 OLLAMA_NOPRUNE=1
ollama serve >/tmp/ollama.log 2>&1 &
server=$!
cleanup() {
    kill "$server" 2>/dev/null || true
    wait "$server" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if ! timeout 60 bash -c 'until ollama list >/dev/null 2>&1; do sleep 0.25; done'; then
    tail -20 /tmp/ollama.log >&2
    exit 1
fi
mkdir /tmp/definitions
for model do
    file="/tmp/definitions/$model.model"
    mkdir -p -- "$(dirname -- "$file")"
    if ! timeout 60 ollama show --modelfile -- "$model" > "$file"; then
        rm -- "$file"
    fi
done
tar -cf - -C /tmp/definitions .
"""


class TransferError(ValueError):
    """The project, archive or Docker volumes cannot be transferred."""


def image_names(namespace: str, *, include_terminal: bool = False) -> tuple[str, ...]:
    images = IMAGES + ("local/open-terminal:latest",) if include_terminal else IMAGES
    if not namespace:
        return images
    return tuple(f"{image.rsplit(':', 1)[0]}:{namespace}" for image in images)


def volume_names(namespace: str) -> dict[str, str]:
    if not namespace:
        return VOLUMES
    return {f"{namespace}-{name}": target for name, target in VOLUMES.items()}


def deployment_project(namespace: str) -> str:
    return namespace or "local-inference"


def expand_types(names, *, chats_available: bool = True) -> tuple[str, ...]:
    """Resolve archive types and aliases. Chats need the WebUI data that owns their accounts and chats."""
    selected = set()
    for name in names:
        if name in ALIASES:
            selected.update(kind for kind in ALIASES[name] if kind != "chats" or chats_available)
        elif name in TYPES:
            selected.add(name)
        else:
            raise TransferError(f"Unknown archive type {name}. Use {', '.join(TYPES)}, project or volumes.")
    if "chats" in selected:
        selected.add("webui")
    return tuple(kind for kind in TYPES if kind in selected)


def default_backup_types(project: Path, *, chats: bool) -> tuple[str, ...]:
    """Back up every available type. The source exists only in a checkout; chats are opt-in."""
    checkout = checkout_paths(project) is not None
    return tuple(kind for kind in TYPES if (kind != "source" or checkout) and (kind != "chats" or chats))


def backup_path(directory: Path, *, namespace: str = "") -> Path:
    name = "_".join(part for part in (f"{datetime.now():%Y%m%d-%H%M}", namespace, BUNDLE_NAME) if part)
    target = directory / name
    sequence = 2
    while target.exists() or target.is_symlink():
        target = directory / f"{name}-{sequence}"
        sequence += 1
    return target


def latest_backup(directory: Path, *, namespace: str = "") -> Path | None:
    prefix = re.escape(f"{namespace}_" if namespace else "")
    folders = re.compile(r"([0-9]{8}-[0-9]{4})_" + prefix + re.escape(BUNDLE_NAME) + r"(?:-([0-9]+))?")
    backups = []
    for path in directory.iterdir() if directory.is_dir() else ():
        if (match := folders.fullmatch(path.name)) and is_bundle(path):
            backups.append((match[1], int(match[2] or "1"), path))
    return max(backups)[2] if backups else None


def is_bundle(directory: Path) -> bool:
    return (directory.is_dir() and (directory / CHECKSUMS).is_file() and not (directory / CHECKSUMS).is_symlink()
            and (directory / LAUNCHER).is_file())


def find_bundle(directory: Path, selection: Path | None) -> Path | None:
    """Return the selected backup folder, the current one, or the folder holding the running launcher."""
    if selection is not None:
        if is_bundle(selection):
            return selection
        return selection.parent if not selection.is_dir() and is_bundle(selection.parent) else None
    if is_bundle(directory):
        return directory
    executable = os.environ.get("LOAF_RELEASE_EXECUTABLE")
    if executable and is_bundle(folder := Path(executable).parent):
        return folder
    return None


def adopt_bundle(folder: Path) -> Path:
    """Make a backup folder a project: move the backup to backup/<name>/ and keep a launcher copy at the root."""
    sums = read_checksums(folder)
    archive = next(name for name in sums if name != LAUNCHER)
    # The archive keeps the folder's original name, which later restores find in the default backup directory.
    target = folder / "backup" / archive.removesuffix(".tar.gz")
    if target.exists() or target.is_symlink():
        raise TransferError(f"Cannot move the backup to {target}, which already exists.")
    target.parent.mkdir(exist_ok=True)
    launcher = folder / LAUNCHER
    with ExitStack() as rollback:
        target.mkdir()
        rollback.callback(target.rmdir)
        copy = folder / f".loaf-launcher-{os.getpid()}"
        rollback.callback(copy.unlink, missing_ok=True)
        shutil.copyfile(launcher, copy)
        copy.chmod(file_modes(folder)[1])
        # The moved backup stays complete: its checksums list the original launcher and archive.
        for name in (archive, LAUNCHER, CHECKSUMS):
            (folder / name).rename(target / name)
            rollback.callback((target / name).rename, folder / name)
        copy.rename(launcher)
        rollback.pop_all()
    return target


def standalone_executable(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as executable:
        header = executable.read(4096)
    return header.startswith(EXECUTABLE_HEADER) and b"\n__LOAF_PAYLOAD__\n\x1f\x8b" in header


def release_executable(project: Path) -> Path:
    configured = os.environ.get("LOAF_RELEASE_EXECUTABLE")
    candidates = [Path(configured)] if configured else [project / "loaf", project / "dist/loaf"]
    for candidate in candidates:
        if standalone_executable(candidate):
            return candidate
    raise TransferError("Standalone release executable missing. Run backup with the release loaf or put it at dist/loaf.")


def file_modes(directory: Path) -> tuple[int, int]:
    """Return the modes for new private files and executables, readable by a shared directory's group."""
    return (0o640, 0o750) if locks.shared_directory(directory) else (0o600, 0o700)


# --- Archive manifest ---

@dataclass(frozen=True)
class Archive:
    """An archive and, for a backup folder, its launcher and recorded checksum."""

    path: Path
    manifest: dict
    executable: Path | None = None
    digest: str | None = None

    @property
    def deployment(self) -> str:
        return self.manifest["deployment"]

    @property
    def items(self) -> dict[str, dict]:
        return {item["type"]: item for item in self.manifest["items"]}


@dataclass(frozen=True)
class RestoredBackup:
    types: tuple[str, ...]
    settings: config.Settings | None


def validate_manifest(data, minimum: int, name: str) -> dict:
    def invalid(detail: str) -> TransferError:
        return TransferError(f"Invalid {MANIFEST} in {name}: {detail}")

    if not isinstance(data, dict) or data.get("format") != ARCHIVE_FORMAT:
        raise invalid("not a loaf archive.")
    version = data.get("version")
    if type(version) is not int:
        raise invalid("missing format version.")
    if not minimum <= version <= ARCHIVE_VERSION:
        accepted = str(ARCHIVE_VERSION) if minimum == ARCHIVE_VERSION else f"{minimum} to {ARCHIVE_VERSION}"
        raise TransferError(
            f"{name} uses archive format {version}. This loaf accepts format {accepted}, limited by "
            "[transfer] backup_version_min. Restore it with the loaf release that created it.")
    deployment = data.get("deployment")
    if not isinstance(deployment, str):
        raise invalid("missing deployment version.")
    try:
        config.namespace(deployment)
    except ValueError as exc:
        raise invalid(str(exc)) from exc
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise invalid("no items.")
    kinds, paths = set(), set()
    for item in items:
        kind = item.get("type") if isinstance(item, dict) else None
        members = item.get("members") if isinstance(item, dict) else None
        if kind not in TYPES or kind in kinds or not isinstance(members, list) or not members:
            raise invalid(f"unexpected item {kind!r}.")
        kinds.add(kind)
        for member in members:
            path = member.get("path") if isinstance(member, dict) else None
            if (path not in MEMBER_NAMES or path in paths or not (path == f"{kind}.tar" or path.startswith(f"{kind}/"))
                    or type(member.get("size")) is not int or member["size"] < 0
                    or not isinstance(member.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", member["sha256"])):
                raise invalid(f"unexpected member {path!r}.")
            paths.add(path)
    if "chats" in kinds and "webui" not in kinds:
        raise invalid("chats without the WebUI data they belong to.")
    return data


def read_manifest(path: Path, minimum: int) -> dict:
    """Read only the first member, which lists the archive's types before any payload."""
    if path.is_symlink() or not path.is_file():
        raise TransferError(f"Backup archive not found: {path}. Use --backup-dir or --archive to locate it.")
    with gzip.open(path, "rb") as compressed, tarfile.open(fileobj=compressed, mode="r|") as archive:
        member = archive.next()
        if member is None or member.name != MANIFEST or not member.isfile() or member.size > MANIFEST_LIMIT:
            raise TransferError(
                f"{path.name} does not begin with {MANIFEST}. Archives from loaf releases before format "
                f"{ARCHIVE_VERSION} need the loaf release that created them; their backup folder contains it.")
        with archive.extractfile(member) as payload:
            data = payload.read()
    try:
        manifest = json.loads(data)
    except ValueError as exc:
        raise TransferError(f"Invalid {MANIFEST} in {path.name}.") from exc
    return validate_manifest(manifest, minimum, path.name)


def read_checksums(directory: Path) -> dict[str, str]:
    sums = {}
    for line in (directory / CHECKSUMS).read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\s]+)", line)
        if not match or match[2] in sums:
            raise TransferError(f"Invalid {CHECKSUMS} in {directory}.")
        sums[match[2]] = match[1]
    archives = [name for name in sums if name != LAUNCHER]
    if LAUNCHER not in sums or len(archives) != 1 or not archives[0].endswith(".tar.gz"):
        raise TransferError(f"{CHECKSUMS} in {directory} must list the launcher and one archive.")
    return sums


def open_archive(selection: Path, *, minimum: int = ARCHIVE_VERSION, verify_launcher: bool = True) -> Archive:
    """Open a backup folder or an archive file and read its manifest."""
    if not selection.is_dir():
        return Archive(selection, read_manifest(selection, minimum))
    if not is_bundle(selection):
        raise TransferError(f"{selection} is not a backup folder with {LAUNCHER} and {CHECKSUMS}.")
    sums = read_checksums(selection)
    name = next(name for name in sums if name != LAUNCHER)
    executable = selection / LAUNCHER
    if executable.is_symlink() or not standalone_executable(executable):
        raise TransferError(f"Standalone backup launcher missing or invalid: {executable}")
    if verify_launcher and file_digest(executable) != sums[LAUNCHER]:
        raise TransferError(f"SHA-256 verification failed for {executable}.")
    return Archive(selection / name, read_manifest(selection / name, minimum), executable, sums[name])


class HashingReader:
    """Hash what a reader consumes and optionally report its progress."""

    def __init__(self, stream, progress: Progress | None = None) -> None:
        self.stream = stream
        self.progress = progress
        self.digest = sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.digest.update(data)
        if self.progress is not None:
            self.progress.advance(len(data))
        return data


def unpack(archive: Archive, staging: Path, select: set[str]) -> None:
    """Check every member against the manifest and stage the selected ones, before any change."""
    expected = {member["path"]: member for item in archive.manifest["items"] for member in item["members"]}
    seen = set()
    with archive.path.open("rb") as raw:
        whole = HashingReader(raw)
        with gzip.GzipFile(fileobj=whole, mode="rb") as compressed:
            with tarfile.open(fileobj=compressed, mode="r|") as members:
                first = members.next()
                with members.extractfile(first) as payload:
                    if json.loads(payload.read()) != archive.manifest:
                        raise TransferError(f"{archive.path.name} changed while it was read.")
                # Iterating the archive would yield the manifest again.
                while (member := members.next()) is not None:
                    if not member.isfile() or member.name not in expected or member.name in seen:
                        raise TransferError(f"Unexpected member in {archive.path.name}: {member.name!r}")
                    if member.size != expected[member.name]["size"]:
                        raise TransferError(f"Size mismatch for {member.name} in {archive.path.name}.")
                    stage = member.name in select
                    if stage:
                        (staging / member.name).parent.mkdir(parents=True, exist_ok=True)
                    target = (staging / member.name).open("xb") if stage else nullcontext()
                    with (
                        target as output,
                        Progress(f"{'Unpack' if stage else 'Check'} {member.name}", member.size) as progress,
                        members.extractfile(member) as payload,
                    ):
                        reader = HashingReader(payload, progress)
                        while block := reader.read(COPY_BUFFER):
                            if output is not None:
                                output.write(block)
                    if reader.digest.hexdigest() != expected[member.name]["sha256"]:
                        raise TransferError(f"SHA-256 verification failed for {member.name}.")
                    seen.add(member.name)
            # Read to EOF so gzip checks its checksum before Docker state can change.
            while compressed.read(COPY_BUFFER):
                pass
        while whole.read(COPY_BUFFER):
            pass
    if seen != set(expected):
        raise TransferError(f"{archive.path.name} is missing {', '.join(sorted(set(expected) - seen))}.")
    if archive.digest is not None and whole.digest.hexdigest() != archive.digest:
        raise TransferError(f"SHA-256 verification failed for {archive.path.name}.")


def member_record(staging: Path, path: str) -> dict:
    file = staging / path
    size = file.stat().st_size
    with Progress(f"Hash {path}", size) as progress, file.open("rb") as payload:
        reader = HashingReader(payload, progress)
        while reader.read(COPY_BUFFER):
            pass
    return {"path": path, "size": size, "sha256": reader.digest.hexdigest()}


def write_archive(staging: Path, pending: Path, manifest: dict, compression_level: int) -> None:
    data = (json.dumps(manifest, indent=2) + "\n").encode()
    with tarfile.open(pending, "w:gz", compresslevel=compression_level, copybufsize=COPY_BUFFER) as archive:
        # The manifest comes first so a reader learns the types before any payload.
        entry = tarfile.TarInfo(MANIFEST)
        entry.size, entry.mode, entry.mtime = len(data), 0o600, int(datetime.now().timestamp())
        archive.addfile(entry, io.BytesIO(data))
        for item in manifest["items"]:
            for member in item["members"]:
                entry = archive.gettarinfo(staging / member["path"], arcname=member["path"])
                with (Progress(f"Compress {member['path']}", entry.size) as progress,
                      (staging / member["path"]).open("rb") as payload):
                    archive.addfile(entry, TrackedStream(payload, progress))


# --- File types ---

def checkout_paths(project: Path) -> set[Path] | None:
    """List the checkout's tracked and unignored files, or None outside a Git checkout."""
    git_dir = project / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        return None
    # Scope Git trust to this checkout and avoid repository fsmonitor hooks.
    files = subprocess.check_output(
        ["git", "-c", f"safe.directory={project.resolve().as_posix()}",
         "-c", "core.fsmonitor=false", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=project,
    )
    return {Path(os.fsdecode(name)) for name in files.split(b"\0") if name}


def write_tar(destination: Path, label: str, entries) -> None:
    with Progress(label) as progress, destination.open("wb") as output:
        stream = TrackedStream(output, progress)
        with tarfile.open(fileobj=stream, mode="w|", bufsize=COPY_BUFFER, copybufsize=COPY_BUFFER) as archive:
            for path, name in entries:
                archive.add(path, arcname=name, recursive=False)


def archive_config(project: Path, config_dir: Path, destination: Path, *, exclude_file: Path | None) -> None:
    """Archive settings and saved Modelfiles."""
    # Settings can come entirely from the system or packaged layers, but the directory must be a deployment.
    if not (config_dir.is_dir() or (project / "docker-project").is_symlink() or checkout_paths(project) is not None):
        raise TransferError("Backup needs a deployment prepared by loaf setup or a Git checkout.")
    files = sorted(path for path in config_dir.rglob("*") if path.suffix in CONFIG_SUFFIXES and path.is_file()
                   and path.resolve() != exclude_file) if config_dir.is_dir() else []
    write_tar(destination, "Archive config.tar",
              ((path, (Path("config") / path.relative_to(config_dir)).as_posix()) for path in files))


def archive_installers(project: Path, destination: Path, *, exclude_file: Path | None) -> None:
    """Archive the cached installers: Python wheels, NVIDIA and Docker Debian packages."""
    paths = set()
    packages = toolkit_files(project)
    wheels = project / PYTHON_WHEEL_DIR
    if (project / PYTHON_PACKAGE_DIR / "pyproject.toml").is_file():
        prepare_python_wheels(project)
    # The optional test wheels from loaf setup tests share the cache with their own record.
    for record in (wheels / WHEEL_CACHE_RECORD, wheels / TEST_WHEEL_RECORD):
        if record.is_file() and not record.is_symlink():
            paths.add(record.relative_to(project))
    paths.update(path.relative_to(project) for path in wheels.glob("*.whl") if path.is_file() and not path.is_symlink())
    paths.update(path.relative_to(project) for path in docker_installer_files(project))
    entries = [(package.resolve(), (TOOLKIT_DIR / package.name).as_posix())
               for package in packages if package.resolve() != exclude_file]
    entries += [(project / relative, relative.as_posix()) for relative in sorted(paths)]
    write_tar(destination, "Archive installers.tar", entries)


def archive_source(project: Path, destination: Path, *, exclude_file: Path | None = None) -> None:
    """Archive the Git history and working tree. Settings and installers have their own types."""
    paths = checkout_paths(project)
    if paths is None:
        raise TransferError("The source type needs a Git checkout. Omit source for this deployment.")
    # Skip backup folders kept inside the checkout.
    bundle_dirs = {path.parent for path in paths
                   if path.name == CHECKSUMS and path.parent != Path(".") and (project / path.parent / LAUNCHER).is_file()}
    project_parts = {"config", INSTALL_CACHE.name, ".venv"}

    def exclude_secret(member):
        return None if project / member.name == exclude_file else member

    with Progress("Archive source.tar") as progress, destination.open("wb") as output:
        stream = TrackedStream(output, progress)
        with tarfile.open(fileobj=stream, mode="w|", bufsize=COPY_BUFFER, copybufsize=COPY_BUFFER) as archive:
            archive.add(project / ".git", arcname=".git", filter=exclude_secret)
            for relative in sorted(paths):
                path = project / relative
                # Staging folders of this and other loaf operations stay out, like the backup folders.
                if (relative.parts[0] in project_parts or any(part.startswith(".loaf-") for part in relative.parts)
                        or path == exclude_file or any(relative.is_relative_to(bundle) for bundle in bundle_dirs)):
                    continue
                if relative == Path(LAUNCHER) and path.is_symlink():
                    continue
                if path.exists() or path.is_symlink():
                    archive.add(path, arcname=relative.as_posix(), recursive=False)


def stage_files(source: Path, target: Path, prefix: str) -> None:
    """Extract regular files below one top-level directory, refusing any other path."""
    with tarfile.open(source, "r:") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if (not member.isfile() or not path.is_relative_to(prefix) or ".." in path.parts or path.is_absolute()
                    or (prefix == "config" and path.suffix not in CONFIG_SUFFIXES)):
                raise TransferError(f"Invalid file in {source.name}: {member.name!r}")
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output, archive.extractfile(member) as payload:
                shutil.copyfileobj(payload, output, length=COPY_BUFFER)


def project_file_targets(source: Path, target: Path) -> list[tuple[Path, Path]]:
    files = []
    for path in sorted(source.rglob("*")) if source.is_dir() else ():
        if not path.is_file():
            continue
        destination = target / path.relative_to(source)
        if destination.resolve() != destination or (destination.exists() and not destination.is_file()):
            raise TransferError(f"Cannot restore a regular file to {destination}.")
        for parent in destination.parents:
            if parent.exists() and not parent.is_dir():
                raise TransferError(f"Cannot restore through {parent}.")
            if parent == target:
                break
        files.append((path, destination))
    return files


def publish_project_files(files: list[tuple[Path, Path]], group: int | None = None) -> None:
    """Publish restored files owner-only, or readable by the group that shares the system deployment."""
    for source, destination in files:
        missing, parent = [], destination.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir()
            if group is not None:
                os.chown(directory, -1, group)
                directory.chmod(0o750)
        with TemporaryDirectory(dir=destination.parent, prefix=".loaf-restore-") as directory:
            replacement = Path(directory) / destination.name
            shutil.copyfile(source, replacement)
            if group is not None:
                os.chown(replacement, -1, group)
            replacement.chmod(0o600 if group is None else 0o640)
            replacement.replace(destination)


def stage_configuration(recovered: Path, lower: tuple[Path, ...], destination: Path, version: str,
                        terminal_key: Path | None = None) -> config.Settings:
    """Load recovered settings over the lower configuration layers and keep the backup's version.

    A given terminal key path stays configured: the system deployment's key does not move with its settings.
    """
    target = recovered / config.CONFIG_NAME
    current = recovered if target.is_file() else destination
    settings = config.load(*lower, current)
    keep_key = terminal_key is not None and settings.terminal_server_secret_file != terminal_key
    if settings.version == version and not keep_key:
        return settings
    parser = ConfigParser(interpolation=None)
    parser.read(current / config.CONFIG_NAME, encoding="utf-8-sig")
    pinned = {("launcher", "version"): version}
    if keep_key:
        pinned[("terminal_server", "secret_file")] = str(terminal_key)
    for (section, key), value in pinned.items():
        if not parser.has_section(section):
            parser.add_section(section)
        parser.set(section, key, value)
    recovered.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as output:
        parser.write(output)
    return config.load(*lower, recovered)


def stage_checkout(source: Path, checkout: Path) -> None:
    """Extract source files into a private tree and refuse writes through archive links."""
    data_filter = getattr(tarfile, "data_filter", None)
    if data_filter is None:
        raise TransferError("Source recovery requires the standalone loaf executable or a Python with tar extraction filters.")

    def checked_member(member, directory):
        path = PurePosixPath(member.name)
        target = checkout / path
        if target.is_symlink() or target.resolve() != target:
            raise TransferError(f"Checkout path goes through a link: {member.name!r}")
        checked = data_filter(member, directory)
        return checked if member.issym() else checked.replace(mode=member.mode & 0o777)

    checkout.mkdir()
    with tarfile.open(source, "r:") as archive:
        members = archive.getmembers()
        seen = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (not path.parts or path.is_absolute() or ".." in path.parts or path in seen
                    or path.parts[0] in CHECKOUT_RESERVED_PATHS or path.parts[0].startswith(".loaf-")):
                raise TransferError(f"Invalid checkout path in backup: {member.name!r}")
            seen.add(path)
        archive.extractall(checkout, members=members, filter=checked_member)
    if not (checkout / PYTHON_PACKAGE_DIR / "pyproject.toml").is_file() or not (checkout / "docs").is_dir():
        raise TransferError("The source type in this backup is not a complete loaf checkout.")


def checkout_targets(checkout: Path, project: Path) -> list[tuple[Path, Path]]:
    """Map the checkout onto the project. Directories merge; archived files and links replace existing ones."""
    def collect(source: Path, target: Path):
        source_dir = source.is_dir() and not source.is_symlink()
        target_dir = target.is_dir() and not target.is_symlink()
        if source_dir and target_dir:
            for entry in source.iterdir():
                yield from collect(entry, target / entry.name)
        elif source_dir and (target.exists() or target.is_symlink()):
            # Never write a directory through a link or over a file.
            raise TransferError(f"Cannot restore the source directory {target} over a file or link.")
        elif target_dir:
            raise TransferError(f"Cannot restore the source file {target} over a directory.")
        else:
            yield source, target

    return [target for entry in checkout.iterdir() for target in collect(entry, project / entry.name)]


def publish_checkout(entries: list[tuple[Path, Path]], replaced: Path) -> None:
    """Move the checkout into place. A failure puts back every file moved or replaced so far."""
    replaced.mkdir()
    with ExitStack() as rollback:
        for index, (source, target) in enumerate(entries):
            if target.exists() or target.is_symlink():
                previous = replaced / str(index)
                target.rename(previous)
                rollback.callback(previous.rename, target)
            source.rename(target)
            rollback.callback(target.rename, source)
        rollback.pop_all()


# --- Docker types ---

def require_inactive_volume(project: Path, volume: str) -> None:
    consumers = subprocess.check_output(
        ["docker", "ps", "--quiet", "--filter", f"volume={volume}"], cwd=project,
    )
    if consumers.strip():
        raise TransferError(f"The {volume} volume is in use. Stop its containers before transferring it.")


def require_helper_image(project: Path, namespace: str) -> None:
    image = image_names(namespace)[0]
    try:
        subprocess.run(["docker", "image", "inspect", image], cwd=project,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError as exc:
        raise TransferError(f"Restoring volumes without images requires the local helper image {image}.") from exc


def volume_command(*, restore: bool = False, namespace: str = "", volumes: dict[str, str] | None = None) -> list[str]:
    """Stream named volumes as one tar whose top-level directories name the volumes."""
    volumes = volume_names(namespace) if volumes is None else volumes
    command = ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only"]
    if restore:
        command.append("-i")
    for volume, directory in volumes.items():
        mount = f"type=volume,source={volume},target=/transfer/{directory},volume-nocopy"
        command += ["--mount", mount if restore else mount + ",readonly"]
    image = image_names(namespace)[0]
    if restore:
        return command + ["--entrypoint", "/bin/sh", image, "-c", RESTORE_VOLUMES,
                          "loaf-restore-volumes", *(f"/transfer/{directory}" for directory in volumes.values())]
    return command + ["--entrypoint", "tar", image, "-cf", "-", "-C", "/transfer", *volumes.values()]


def copy_payload(read: Callable[[int], bytes], destination, progress: Progress) -> None:
    while block := read(COPY_BUFFER):
        if destination is not None:
            destination.write(block)
        progress.advance(len(block))


def docker_archive(project: Path, command: list[str], path: Path, label: str, *, send: bool = False) -> None:
    total = path.stat().st_size if send else None
    # Preserve Docker diagnostics without letting them overwrite the live bar.
    with TemporaryFile() as log:
        try:
            with (
                Progress(label, total, docker=True) as progress,
                path.open("rb" if send else "wb", buffering=COPY_BUFFER) as payload,
                subprocess.Popen(
                    command, cwd=project, stdin=subprocess.PIPE if send else subprocess.DEVNULL,
                    stdout=log if send else subprocess.PIPE, stderr=log,
                ) as process,
            ):
                if send:
                    try:
                        with process.stdin as pipe:
                            copy_payload(payload.read, pipe, progress)
                    except BrokenPipeError:
                        # Docker can reject an archive before reading stdin.
                        # Let its exit status and diagnostics explain why.
                        pass
                else:
                    with process.stdout as pipe:
                        # Report available pipe data without waiting for a full buffer.
                        copy_payload(pipe.read1, payload, progress)
                if status := process.wait():
                    raise subprocess.CalledProcessError(status, command)
        finally:
            log.seek(0)
            for line in log:
                sys.stderr.write(line.decode("utf-8", errors="replace"))
            sys.stderr.flush()


def modelfile_definition(data: bytes) -> tuple[list[tuple[str, str, str]], dict[str, list[str]]]:
    """Compare saved commands while ignoring comments and parameter key order."""
    commands = []
    parameters: dict[str, list[str]] = {}
    lines = iter(data.decode("utf-8").replace("\r\n", "\n").split("\n"))
    for line in lines:
        words = line.strip().split(maxsplit=1)
        if not words or words[0].startswith("#"):
            continue
        if len(words) != 2:
            raise ValueError("Expected a Modelfile instruction and value.")
        instruction, value = words
        instruction = instruction.upper()
        qualifier = ""
        if instruction in ("PARAMETER", "MESSAGE"):
            qualifier, value = value.split(maxsplit=1)
        if value.startswith('"'):
            quote = '"""' if value.startswith('"""') else '"'
            value = value[len(quote):]
            while not value.endswith(quote):
                continuation = next(lines, None)
                if continuation is None:
                    raise ValueError("Unclosed quoted Modelfile value.")
                value += "\n" + continuation
            value = value[:-len(quote)]
        if instruction == "PARAMETER":
            parameters.setdefault(qualifier, []).append(value)
        else:
            commands.append((instruction, qualifier, value))
    return commands, parameters


def check_backup_modelfiles(project: Path, models: Path, staging: Path, namespace: str) -> list[str]:
    """Compare the saved Modelfiles in a model snapshot with Ollama's current definitions."""
    prefix = PurePosixPath("config")
    blobs = PurePosixPath("/ollama/models/blobs")
    problems = []
    with tarfile.open(models, "r:") as snapshot:
        entries = {PurePosixPath(member.name): member for member in snapshot}
        recipes = {}
        for path, member in entries.items():
            if not path.is_relative_to(prefix) or path.suffix != ".model" or member.isdir():
                continue
            model = path.relative_to(prefix).as_posix().removesuffix(".model")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)*", model):
                problems.append(f"{path}: invalid model name.")
                continue
            if not member.isfile():
                problems.append(f"{path}: expected a regular Modelfile.")
                continue
            try:
                with snapshot.extractfile(member) as recipe:
                    commands, parameters = modelfile_definition(recipe.read())
            except ValueError as exc:
                problems.append(f"{path}: {exc}")
                continue
            for instruction, _, value in commands:
                target = PurePosixPath(value)
                if instruction in ("FROM", "ADAPTER", "DRAFT") and target.is_relative_to(blobs):
                    blob = entries.get(PurePosixPath("models/blobs", target.name))
                    if blob is None or not (blob.isfile() or blob.islnk()):
                        problems.append(f"{path}: missing model blob {target.name}.")
            recipes[model] = commands, parameters
    if not recipes:
        return problems

    current = staging / "modelfiles.tar"
    volume = f"{namespace}-ollama" if namespace else "ollama"
    command = [
        "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
        "--tmpfs", "/tmp", "--mount", f"type=volume,source={volume},target=/ollama,volume-nocopy,readonly",
        "--entrypoint", "/bin/bash", image_names(namespace)[1], "-c", SHOW_SAVED_MODELFILES,
        "loaf-check-modelfiles", *recipes,
    ]
    try:
        docker_archive(project, command, current, "Check saved Modelfiles")
        with tarfile.open(current, "r:") as definitions:
            cached = {PurePosixPath(member.name): member for member in definitions if member.isfile()}
            for model, saved in recipes.items():
                name = PurePosixPath(model + ".model")
                if name not in cached:
                    problems.append(f"{prefix / name}: Ollama could not read the current registration.")
                    continue
                try:
                    with definitions.extractfile(cached[name]) as recipe:
                        current_definition = modelfile_definition(recipe.read())
                except ValueError as exc:
                    problems.append(f"{prefix / name}: could not compare the current Ollama definition ({exc}).")
                    continue
                if current_definition != saved:
                    problems.append(f"{prefix / name}: differs from the current Ollama definition.")
    except (OSError, subprocess.CalledProcessError, tarfile.TarError):
        problems.append("Could not compare saved Modelfiles with the model store.")
    finally:
        current.unlink(missing_ok=True)
    return problems


def saved_image_tags(path: Path) -> dict[str, set[str]]:
    """Return each image tag in a docker image save archive with its platforms."""
    try:
        with tarfile.open(path, "r:") as images:
            with images.extractfile("manifest.json") as manifest:
                entries = json.load(manifest)
            platforms = {}
            for image in entries:
                with images.extractfile(image["Config"]) as metadata:
                    platform = json.load(metadata)
                fields = [platform["os"], platform["architecture"]]
                if platform.get("variant"):
                    fields.append(platform["variant"])
                for tag in image["RepoTags"] or ():
                    platforms.setdefault(tag, set()).add("/".join(fields))
    except (AttributeError, KeyError, ValueError, TypeError) as exc:
        raise TransferError(f"Invalid Docker image manifest in {path.name}.") from exc
    return platforms


def image_platforms(path: Path, namespace: str) -> dict[str, set[str]]:
    platforms = saved_image_tags(path)
    core = set(image_names(namespace))
    if platforms.keys() not in (core, set(image_names(namespace, include_terminal=True))):
        selection = namespace or "the default namespace"
        raise TransferError(f"Backup image tags do not match {selection}. Select the matching --version.")
    return platforms


def validate_volume_payload(path: Path, volumes: set[str]) -> None:
    """Accept only the named volume directories, each present, without links leaving their volume."""
    missing = set(volumes)
    entries, links, targets = set(), set(), set()
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if not name.parts or name.parts[0] not in volumes or ".." in name.parts or name in entries:
                raise TransferError(f"Unexpected path in {path.name}: {member.name!r}")
            if member.ischr() or member.isblk() or (len(name.parts) == 1 and not member.isdir()):
                raise TransferError(f"Unexpected file type in {path.name}: {member.name!r}")
            entries.add(name)
            if len(name.parts) == 1:
                missing.discard(name.name)
            if member.issym() or member.islnk():
                links.add(name)
            if member.islnk():
                target = PurePosixPath(member.linkname)
                if not target.parts or target.parts[0] != name.parts[0] or ".." in target.parts:
                    raise TransferError(f"Invalid hard link in {path.name}: {member.name!r}")
                targets.add(target)
    if any(not links.isdisjoint(entry.parents) for entry in entries | targets):
        raise TransferError(f"A path in {path.name} traverses an archived link.")
    if missing:
        raise TransferError(f"{path.name} is missing data for {', '.join(sorted(missing))}.")


MODEL_TRANSFER_SETUP = """\
set -eu
command -v rsync >/dev/null || { echo 'Run loaf build to add rsync to the transfer image.' >&2; exit 1; }
"""

SNAPSHOT_MODELS = MODEL_TRANSFER_SETUP + """\
mkdir /staging/models
# Capture the manifest set before copying the immutable blobs it refers to.
rsync -a --exclude=/blobs/ -- /ollama/models/ /staging/models/
if [ -d /ollama/config ]; then
    rsync -a --exclude='*.model.tmp.*' -- /ollama/config /staging/
fi
if [ -d /ollama/models/blobs ]; then
    rsync -a --exclude='*-partial*' -- /ollama/models/blobs /staging/models/
fi
cd /staging
set -- models
if [ -d config ]; then set -- "$@" config; fi
tar -cf - "$@"
"""

IMPORT_MODELS = MODEL_TRANSFER_SETUP + """\
tar -xpf - -C /staging
mkdir -p /staging/models/blobs /staging/config /ollama/models/blobs /ollama/config
scratch=$(mktemp -d /ollama/.loaf-transfer.XXXXXX)
trap 'rm -rf -- "$scratch"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
rsync -a --ignore-existing --temp-dir="$scratch" --exclude='*-partial*' -- /staging/models/blobs/ /ollama/models/blobs/
rsync -a --checksum "$@" --temp-dir="$scratch" -- /staging/config/ /ollama/config/
# Publish complete manifests before deleting any blobs that old manifests use.
rsync -a --checksum "$@" --temp-dir="$scratch" --exclude=/blobs/ -- /staging/models/ /ollama/models/
if [ "$#" -gt 0 ]; then
    rsync -a --ignore-existing "$@" --delete-excluded --temp-dir="$scratch" --exclude='*-partial*' -- /staging/models/blobs/ /ollama/models/blobs/
fi
"""


def model_blob(entries: dict[PurePosixPath, tarfile.TarInfo], digest: str) -> tarfile.TarInfo:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Invalid blob digest.")
    blob = entries.get(PurePosixPath("models", "blobs", digest.replace(":", "-")))
    if blob is None or not blob.isfile():
        raise ValueError(f"Missing or incomplete blob {digest}.")
    return blob


def read_model_manifest(archive: tarfile.TarFile, member: tarfile.TarInfo, digest: str | None = None) -> dict:
    if member.size > COPY_BUFFER:
        raise ValueError("Model manifest exceeds 1 MiB.")
    with archive.extractfile(member) as payload:
        data = payload.read()
    if digest is not None and "sha256:" + sha256(data).hexdigest() != digest:
        raise ValueError(f"Manifest blob digest mismatch for {digest}.")
    definition = json.loads(data)
    if not isinstance(definition, dict):
        raise ValueError("Expected a model manifest object.")
    return definition


def validate_model_layers(entries: dict[PurePosixPath, tarfile.TarInfo], definition: dict) -> None:
    for layer in (definition["config"], *definition["layers"]):
        digest = layer["digest"]
        blob = model_blob(entries, digest)
        if type(layer["size"]) is not int or blob.size != layer["size"]:
            raise ValueError(f"Missing or incomplete blob {digest}.")


def validate_model_archive(path: Path) -> None:
    with tarfile.open(path, "r:") as archive:
        entries = {}
        manifests = []
        for member in archive:
            name = PurePosixPath(member.name)
            # A v2 tag path ends in registry/namespace/model/tag.
            v2_manifest = name.parts[:2] == ("models", "manifests-v2") and len(name.parts) == 6
            if (not name.parts or name.parts[0] not in ("models", "config") or ".." in name.parts
                    or "\\" in member.name or name in entries
                    or not (member.isdir() or member.isfile() or (v2_manifest and member.issym()))):
                raise TransferError(f"Unexpected model archive member: {member.name!r}")
            if not member.isdir() and len(name.parts) == 1:
                raise TransferError(f"Expected a directory for {member.name!r}.")
            entries[name] = member
            if (v2_manifest and not member.isdir()) or (member.isfile() and name.parts[:2] == ("models", "manifests")):
                manifests.append((name, member))
        if PurePosixPath("models") not in entries:
            raise TransferError("The model archive is missing the models directory.")
        for name in entries:
            for parent in name.parents:
                if parent in entries and not entries[parent].isdir():
                    raise TransferError(f"Model archive member {str(name)!r} has a non-directory parent {str(parent)!r}.")
        for name, manifest in manifests:
            try:
                if manifest.issym():
                    filename = PurePosixPath(manifest.linkname).name
                    expected = posixpath.relpath(f"models/blobs/{filename}", name.parent.as_posix())
                    if not filename.startswith("sha256-") or manifest.linkname != expected:
                        raise ValueError("Expected a relative link to a manifest blob.")
                    digest = filename.replace("-", ":", 1)
                    definition = read_model_manifest(archive, model_blob(entries, digest), digest)
                else:
                    definition = read_model_manifest(archive, manifest)

                if definition.get("mediaType") == MANIFEST_LIST_TYPE:
                    children = definition["manifests"]
                    if not isinstance(children, list) or not children:
                        raise ValueError("Expected a non-empty manifest list.")
                    for child in children:
                        if not isinstance(child, dict):
                            raise ValueError("Expected a child manifest object.")
                        if "digest" in child:
                            digest = child["digest"]
                            child = read_model_manifest(archive, model_blob(entries, digest), digest)
                        validate_model_layers(entries, child)
                else:
                    validate_model_layers(entries, definition)
            except (KeyError, TypeError, ValueError) as exc:
                raise TransferError(f"Invalid model manifest {manifest.name!r}. {exc}") from exc


def model_volume_command(project: Path, namespace: str, *, restore: bool = False, delete: bool = False) -> list[str]:
    volume = f"{namespace}-ollama" if namespace else "ollama"
    if not restore:
        subprocess.check_output(["docker", "volume", "inspect", volume], cwd=project)
    consumers = subprocess.check_output([
        "docker", "ps", "--filter", f"volume={volume}", "--format",
        '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}',
    ], cwd=project).decode().splitlines()
    deployment = deployment_project(namespace)
    serving = {f"{deployment}/ollama-{profile}" for profile in ("connected", "offline")}
    if len(consumers) > 1 or any(consumer not in serving for consumer in consumers):
        raise TransferError(f"The {volume} volume is in use by another container. Stop it before transferring models.")
    mount = f"type=volume,source={volume},target=/ollama,volume-nocopy"
    command = ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only"]
    if restore:
        command += ["-i"]
    else:
        mount += ",readonly"
    command += [
        "--mount", mount, "--mount", "type=volume,target=/staging",
        "--entrypoint", "/bin/sh", image_names(namespace)[0], "-c",
        IMPORT_MODELS if restore else SNAPSHOT_MODELS,
    ]
    if delete:
        command += ["loaf-model-import", "--delete-delay"]
    return command


# --- Terminal workspaces: account folders, chat folders and chat images ---

def workspace_prefix(namespace: str) -> str:
    project = sha256(deployment_project(namespace).encode()).hexdigest()[:16]
    return f"loaf-tools-{project}-"


def validate_workspace_names(names, namespace: str) -> tuple[str, ...]:
    prefix = workspace_prefix(namespace)
    if (not isinstance(names, list)
            or any(not isinstance(name, str) or not re.fullmatch(prefix + r"[0-9a-f]{64}", name) for name in names)
            or len(set(names)) != len(names)):
        raise TransferError("Invalid terminal workspace volume names for the selected deployment.")
    return tuple(sorted(names))


def workspace_labels(namespace: str, name: str) -> dict[str, str]:
    return {
        WORKSPACE_PROJECT_LABEL: deployment_project(namespace),
        WORKSPACE_OWNER_LABEL: name.removeprefix(workspace_prefix(namespace)),
    }


def chat_key(chat: dict) -> str:
    return sha256(f"{chat['owner']}/{chat['id']}".encode()).hexdigest()


def chat_volume(namespace: str, chat: dict) -> str:
    return f"{workspace_prefix(namespace)}{chat_key(chat)}-chat"


def chat_image(namespace: str, chat: dict) -> str:
    return f"{CHAT_IMAGE_REPOSITORY}:{workspace_prefix(namespace)}{chat_key(chat)}"


def chat_labels(namespace: str, chat: dict) -> dict[str, str]:
    return {CHAT_PROJECT_LABEL: deployment_project(namespace), CHAT_ID_LABEL: chat["id"], CHAT_OWNER_LABEL: chat["owner"]}


def validate_chats(chats) -> list[dict]:
    if not isinstance(chats, list) or any(
        not isinstance(chat, dict) or set(chat) != {"id", "owner"} or not isinstance(chat["id"], str)
        or not re.fullmatch(r"[!-~]{1,128}", chat["id"]) or chat["id"].startswith(UNSAVED_CHAT_PREFIXES)
        or not isinstance(chat["owner"], str) or not re.fullmatch(r"[0-9a-f]{64}", chat["owner"])
        for chat in chats
    ) or len({chat_key(chat) for chat in chats}) != len(chats):
        raise TransferError("Invalid terminal chat list in the backup.")
    return chats


def list_terminal_workspaces(project: Path, namespace: str) -> tuple[str, ...]:
    label = f"{WORKSPACE_PROJECT_LABEL}={deployment_project(namespace)}"
    names = subprocess.check_output(["docker", "volume", "ls", "--quiet", "--filter", f"label={label}"], cwd=project)
    workspaces = validate_workspace_names(names.decode().split(), namespace)
    if workspaces:
        volumes = json.loads(subprocess.check_output(["docker", "volume", "inspect", *workspaces], cwd=project))
        if {volume["Name"]: volume["Labels"] for volume in volumes} != {
            name: workspace_labels(namespace, name) for name in workspaces
        }:
            raise TransferError("Terminal workspace volumes do not match the deployment's ownership labels.")
    return workspaces


def list_saved_chats(project: Path, namespace: str) -> list[dict]:
    """List the saved chats' folders. Temporary chats stay behind."""
    label = f"{CHAT_PROJECT_LABEL}={deployment_project(namespace)}"
    names = subprocess.check_output(["docker", "volume", "ls", "--quiet", "--filter", f"label={label}"], cwd=project)
    if not names.split():
        return []
    chats = []
    for volume in json.loads(subprocess.check_output(["docker", "volume", "inspect", *names.decode().split()], cwd=project)):
        labels = volume["Labels"] or {}
        chat = {"id": labels.get(CHAT_ID_LABEL, ""), "owner": labels.get(CHAT_OWNER_LABEL, "")}
        if chat["id"].startswith(UNSAVED_CHAT_PREFIXES):
            continue
        validate_chats([chat])
        if volume["Name"] != chat_volume(namespace, chat) or labels != chat_labels(namespace, chat):
            raise TransferError("Terminal chat volumes do not match the deployment's ownership labels.")
        chats.append(chat)
    return sorted(chats, key=chat_key)


def list_chat_images(project: Path, namespace: str, chats: list[dict]) -> list[str]:
    output = subprocess.check_output([
        "docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}",
        "--filter", f"label={CHAT_IMAGE_PROJECT_LABEL}={deployment_project(namespace)}",
        "--filter", f"label={CHAT_IMAGE_TEMPORARY_LABEL}=false",
    ], cwd=project).decode().split()
    return sorted(tag for tag in (chat_image(namespace, chat) for chat in chats) if tag in output)


def create_labeled_volume(project: Path, name: str, labels: dict[str, str], driver: str, options: dict[str, str]) -> None:
    command = ["docker", "volume", "create", "--driver", driver]
    for label, value in labels.items():
        command += ["--label", f"{label}={value}"]
    for option, value in options.items():
        command += ["--opt", f"{option}={value}"]
    subprocess.check_output([*command, name], cwd=project)
    volume, = json.loads(subprocess.check_output(["docker", "volume", "inspect", name], cwd=project))
    if volume["Labels"] != labels or volume["Driver"] != driver or (volume["Options"] or {}) != options:
        raise TransferError(f"Terminal volume {name} does not match the deployment's ownership or storage settings.")


def create_workspace_volumes(project: Path, namespace: str, workspaces: tuple[str, ...],
                             driver: str, options: dict[str, str]) -> None:
    for name in workspaces:
        create_labeled_volume(project, name, workspace_labels(namespace, name), driver, options)


def matching_terminal_key(path: Path, key: bytes) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    existing = config.read_terminal_secret(path)
    if existing.rstrip(b"\r\n") != key.rstrip(b"\r\n"):
        raise TransferError(f"Terminal server key at {path} differs from the backup.")
    return True


@contextmanager
def restore_terminal_key(path: Path, key: bytes, *, group: str | None = None):
    if matching_terminal_key(path, key):
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=TERMINAL_KEY_STAGING) as pending:
        pending.write(key)
        pending.flush()
        protect_terminal_key(pending.name, group)
        try:
            # Publish a complete key file without replacing an existing credential.
            os.link(pending.name, path)
        except FileExistsError:
            if not matching_terminal_key(path, key):
                raise
            yield
        else:
            try:
                yield
            except BaseException:
                if path.exists() and path.samefile(pending.name):
                    path.unlink()
                raise


@dataclass(frozen=True)
class Chats:
    key: bytes
    accounts: tuple[str, ...]
    chats: list[dict]
    images: tuple[str, ...]

    def volumes(self, namespace: str) -> dict[str, dict[str, str]]:
        volumes = {name: workspace_labels(namespace, name) for name in self.accounts}
        volumes.update({chat_volume(namespace, chat): chat_labels(namespace, chat) for chat in self.chats})
        return volumes


def read_chats(item: dict, staging: Path, namespace: str) -> Chats:
    key = (staging / CHAT_KEY).read_bytes()
    if not config.TERMINAL_KEY_PATTERN.fullmatch(key):
        raise TransferError("Invalid terminal server key in the backup.")
    chats = validate_chats(item.get("chats"))
    images = item.get("images")
    if not isinstance(images, list) or not set(images) <= {chat_image(namespace, chat) for chat in chats}:
        raise TransferError("Invalid terminal chat images in the backup. Select the matching --version.")
    result = Chats(key, validate_workspace_names(item.get("accounts"), namespace), chats, tuple(sorted(images)))
    validate_volume_payload(staging / CHAT_VOLUMES, set(result.volumes(namespace)))
    if images and set(saved_image_tags(staging / CHAT_IMAGES)) != set(images):
        raise TransferError("The chat images in the backup do not match its chat list.")
    if (CHAT_IMAGES in {member["path"] for member in item["members"]}) != bool(images):
        raise TransferError("The chat images in the backup do not match its chat list.")
    return result


# --- Backup ---

def backup(
    project: Path,
    destination: Path,
    prepare_services: Callable[[], None],
    *,
    types: tuple[str, ...],
    namespace: str = "",
    version: str = "",
    compression_level: int = config.DEFAULT_COMPRESSION_LEVEL,
    terminal_key: Path | None = None,
    include_terminal_image: bool = True,
    config_dir: Path | None = None,
) -> None:
    """Write one archive: loaf.json first, then the members of each selected type."""
    if destination.is_relative_to(project / ".git"):
        raise TransferError("Choose a backup directory outside .git.")
    if not types or set(types) - set(TYPES):
        raise TransferError(f"Select archive types from {', '.join(TYPES)}.")
    if "chats" in types and "webui" not in types:
        raise TransferError("Chats need the WebUI data that owns their accounts and chats. Add webui.")
    terminal_key = terminal_key or project / config.TERMINAL_SECRET
    key = config.read_terminal_secret(terminal_key) if "chats" in types else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    items = []
    with TemporaryDirectory(prefix=".loaf-backup-", dir=destination.parent) as folder:
        staging = Path(folder)

        def add(kind: str, *paths: str, **metadata) -> None:
            items.append({"type": kind, **metadata, "members": [member_record(staging, path) for path in paths]})

        if "source" in types:
            print("Collecting source files…", file=sys.stderr, flush=True)
            archive_source(project, staging / "source.tar", exclude_file=terminal_key)
            add("source", "source.tar")
        if "config" in types:
            archive_config(project, config_dir or project / "config", staging / "config.tar", exclude_file=terminal_key)
            add("config", "config.tar")
        if "installers" in types:
            print("Collecting cached installers…", file=sys.stderr, flush=True)
            archive_installers(project, staging / "installers.tar", exclude_file=terminal_key)
            add("installers", "installers.tar")
        if "images" in types:
            images = image_names(namespace)
            if include_terminal_image:
                terminal_image = image_names(namespace, include_terminal=True)[-1]
                if subprocess.check_output(
                    ["docker", "image", "ls", "--quiet", "--filter", f"reference={terminal_image}"], cwd=project,
                ).strip():
                    images += (terminal_image,)
            docker_archive(project, ["docker", "image", "save", *images], staging / "images.tar", "Save images.tar")
            add("images", "images.tar", images=list(images))
        if {"models", *SERVICE_TYPES} & set(types):
            print("Preparing services for the backup…", file=sys.stderr, flush=True)
            prepare_services()
        if "models" in types:
            docker_archive(project, model_volume_command(project, namespace), staging / "models.tar", "Save models.tar")
            validate_model_archive(staging / "models.tar")
            for problem in check_backup_modelfiles(project, staging / "models.tar", staging, namespace):
                print(f"Warning: {problem}", file=sys.stderr, flush=True)
            add("models", "models.tar")
        if "webui" in types:
            webui = next(name for name, role in volume_names(namespace).items() if role == "webui")
            require_inactive_volume(project, webui)
            docker_archive(project, volume_command(namespace=namespace, volumes={webui: "webui"}),
                           staging / "webui.tar", "Save webui.tar")
            add("webui", "webui.tar")
        if "chats" in types:
            accounts = list_terminal_workspaces(project, namespace)
            chats = list_saved_chats(project, namespace)
            images = list_chat_images(project, namespace, chats)
            volumes = [*accounts, *(chat_volume(namespace, chat) for chat in chats)]
            for volume in volumes:
                require_inactive_volume(project, volume)
            (staging / "chats").mkdir()
            if volumes:
                docker_archive(project, volume_command(namespace=namespace, volumes={name: name for name in volumes}),
                               staging / CHAT_VOLUMES, f"Save {CHAT_VOLUMES}")
            else:
                # tar refuses to create an empty archive.
                tarfile.open(staging / CHAT_VOLUMES, "w").close()
            members = [CHAT_VOLUMES]
            if images:
                docker_archive(project, ["docker", "image", "save", *images], staging / CHAT_IMAGES, f"Save {CHAT_IMAGES}")
                members.append(CHAT_IMAGES)
            (staging / CHAT_KEY).write_bytes(key)
            add("chats", *members, CHAT_KEY, accounts=list(accounts), chats=chats, images=images)

        manifest = {
            "format": ARCHIVE_FORMAT, "version": ARCHIVE_VERSION, "created": datetime.now().astimezone().isoformat(),
            "loaf": assets.release_version(), "deployment": version, "items": items,
        }
        # Publish only after all payloads are complete, preserving any old backup
        # if a command fails or the operation is interrupted.
        pending = staging / destination.name
        write_archive(staging, pending, manifest, compression_level)
        pending.chmod(file_modes(destination.parent)[0])
        pending.replace(destination)


def backup_bundle(project: Path, destination: Path, prepare_services: Callable[[], None], *,
                  version: str, executable: Path | None = None, **options) -> None:
    """Publish a backup folder with the archive, the launcher that can restore it and their checksums."""
    if destination.is_relative_to(project / ".git"):
        raise TransferError("Choose a backup directory outside .git.")
    if executable is None:
        executable = release_executable(project)
    elif not standalone_executable(executable):
        raise TransferError(f"The build did not produce a standalone executable at {executable}.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    private, runnable = file_modes(destination.parent)
    with TemporaryDirectory(prefix=".loaf-backup-", dir=destination.parent) as folder:
        bundle = Path(folder) / destination.name
        bundle.mkdir()
        bundle.chmod(runnable | (bundle.stat().st_mode & stat.S_ISGID))
        copy = bundle / LAUNCHER
        shutil.copyfile(executable, copy)
        copy.chmod(runnable)
        archive = bundle / f"{destination.name}.tar.gz"
        backup(project, archive, prepare_services, namespace=config.namespace(version), version=version, **options)
        sums = bundle / CHECKSUMS
        sums.write_text(f"{file_digest(copy)}  {LAUNCHER}\n{file_digest(archive)}  {archive.name}\n", encoding="utf-8")
        sums.chmod(private)
        bundle.rename(destination)


# --- Verify and restore ---

def describe(archive: Archive) -> list[str]:
    lines = [f"Archive format {archive.manifest['version']}, created {archive.manifest.get('created', 'unknown')} "
             f"by loaf {archive.manifest.get('loaf', 'unknown')} for "
             f"{config.namespace(archive.deployment) or 'the default deployment'}."]
    for item in archive.manifest["items"]:
        size = sum(member["size"] for member in item["members"])
        lines.append(f"  {item['type']:<10} {size:>15,} bytes")
    return lines


def verify_backup(archive: Archive, namespace: str) -> None:
    with TemporaryDirectory(prefix="loaf-verify-") as folder:
        staging = Path(folder)
        unpack(archive, staging, {"images.tar"})
        platforms = image_platforms(staging / "images.tar", namespace) if "images" in archive.items else {}
    print(f"Verified SHA-256 hashes for all members of {archive.path}")
    for line in describe(archive):
        print(line)
    for image, supported in sorted(platforms.items()):
        print(f"  {image}  {', '.join(sorted(supported))}")


def setup_from_archive(archive: Archive, project: Path, *, installers: bool) -> bytes | None:
    """Recover the archived terminal key and, on request, the cached installers before setup."""
    items = archive.items
    if installers and "installers" not in items:
        raise TransferError("The backup has no installers type containing cached host installers.")
    with TemporaryDirectory(prefix=".loaf-restore-", dir=project) as folder:
        staging = Path(folder)
        unpack(archive, staging, {CHAT_KEY, *(("installers.tar",) if installers else ())})
        if installers:
            stage_files(staging / "installers.tar", staging / "files", INSTALL_CACHE.as_posix())
            publish_project_files(project_file_targets(staging / "files", project))
        key = staging / CHAT_KEY
        if key.is_file():
            data = key.read_bytes()
            if not config.TERMINAL_KEY_PATTERN.fullmatch(data):
                raise TransferError("Invalid terminal server key in the backup.")
            return data
    return None


def restore(project: Path, archive: Archive, types: tuple[str, ...] | None = None, *, namespace: str = "",
            settings: config.Settings | None = None, configuration: tuple[tuple[Path, ...], Path, str] | None = None,
            delete: bool = False, system: bool = False) -> RestoredBackup:
    """Verify the whole archive, then restore the selected types in order.

    The system deployment shares restored settings and installers with its directories' group,
    keeps its terminal key path and lets that group read a restored key.
    """
    items = archive.items
    requested = expand_types(types) if types else tuple(kind for kind in TYPES if kind in items)
    missing = [kind for kind in requested if kind not in items]
    if missing:
        raise TransferError(f"{archive.path.name} does not contain {', '.join(missing)}.")
    settings = settings or config.Settings()
    config_dir = configuration[1] if configuration else project / "config"
    project.mkdir(parents=True, exist_ok=True)
    # Stage files in the project so the archive can stay on read-only media.
    with TemporaryDirectory(prefix=".loaf-restore-", dir=project) as folder:
        staging = Path(folder)
        print(f"Checking and unpacking {archive.path}…", file=sys.stderr, flush=True)
        unpack(archive, staging, {member["path"] for kind in requested for member in items[kind]["members"]})

        # Check everything before the first change.
        if "images" in requested:
            image_platforms(staging / "images.tar", namespace)
        if "models" in requested:
            validate_model_archive(staging / "models.tar")
        if "webui" in requested:
            validate_volume_payload(staging / "webui.tar", {"webui"})
        chats = read_chats(items["chats"], staging, namespace) if "chats" in requested else None
        checkout = staging / "checkout"
        if "source" in requested:
            stage_checkout(staging / "source.tar", checkout)
        files = staging / "files"
        restored_settings = None
        if "config" in requested:
            stage_files(staging / "config.tar", files, "config")
            if configuration is not None:
                lower, _, version = configuration
                system_key = config.terminal_secret_path(project, settings.terminal_server_secret_file) if system else None
                settings = restored_settings = stage_configuration(files / "config", lower, config_dir, version,
                                                                   system_key)
        if "installers" in requested:
            stage_files(staging / "installers.tar", files, INSTALL_CACHE.as_posix())
        checkout_entries = checkout_targets(checkout, project) if "source" in requested else []
        config_targets = project_file_targets(files / "config", config_dir)
        installer_targets = project_file_targets(files / INSTALL_CACHE, project / INSTALL_CACHE)
        terminal_key = config.terminal_secret_path(project, settings.terminal_server_secret_file)
        # The key keeps its own content and mode; a restored settings file never replaces it.
        if any(path == terminal_key for _, path in config_targets + installer_targets):
            raise TransferError("The backup uses the terminal key path for a configuration file.")
        if chats is not None:
            matching_terminal_key(terminal_key, chats.key)
        if {"models", "webui", "chats"} & set(requested) and "images" not in requested:
            require_helper_image(project, namespace)
        webui = next(name for name, role in volume_names(namespace).items() if role == "webui")
        if "webui" in requested:
            require_inactive_volume(project, webui)
        chat_volumes = chats.volumes(namespace) if chats is not None else {}
        for volume in chat_volumes:
            require_inactive_volume(project, volume)
        model_command = model_volume_command(project, namespace, restore=True, delete=delete) if "models" in requested else None

        publish_checkout(checkout_entries, staging / "replaced")
        publish_project_files(config_targets, config_dir.stat().st_gid if system else None)
        publish_project_files(installer_targets, project.stat().st_gid if system else None)
        if "images" in requested:
            docker_archive(project, ["docker", "image", "load"], staging / "images.tar", "Load images.tar", send=True)
        if model_command is not None:
            docker_archive(project, model_command, staging / "models.tar", "Restore models.tar", send=True)
        if "webui" in requested:
            docker_archive(project, volume_command(restore=True, namespace=namespace, volumes={webui: "webui"}),
                           staging / "webui.tar", "Restore webui.tar", send=True)
        if chats is not None:
            driver, options = settings.terminal_server_volume_driver, json.loads(settings.terminal_server_volume_options)
            for volume, labels in chat_volumes.items():
                create_labeled_volume(project, volume, labels, driver, options)
            if chats.images:
                docker_archive(project, ["docker", "image", "load"], staging / CHAT_IMAGES, f"Load {CHAT_IMAGES}",
                               send=True)
            with restore_terminal_key(terminal_key, chats.key, group=SYSTEM_GROUP if system else None):
                if chat_volumes:
                    docker_archive(project, volume_command(restore=True, namespace=namespace,
                                                           volumes={name: name for name in chat_volumes}),
                                   staging / CHAT_VOLUMES, f"Restore {CHAT_VOLUMES}", send=True)
            print(f"Terminal server key available at {terminal_key}.")
        return RestoredBackup(requested, restored_settings)
