"""Transfer deployment data in one archive whose first member lists its typed contents."""

from __future__ import annotations

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
from typing import Callable, Iterable

from . import assets, config, locks
from .progress import Progress, TrackedStream
from .setup import (
    INSTALL_CACHE, PYTHON_PACKAGE_DIR, PYTHON_RUNTIME_DIR, PYTHON_WHEEL_DIR, SYSTEM_GROUP, TEST_WHEEL_RECORD,
    TOOLKIT_DIR, WHEEL_CACHE_RECORD, docker_installer_files, file_digest, prepare_python_wheels, protect_terminal_key,
    toolkit_files,
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
TYPES = ("source", "config", "installers", "images", "models", "webui", "terminal")
ALIASES = {"project": ("config", "installers"), "volumes": ("models", "webui", "terminal")}
# Types without namespaced Docker resources or settings can restore into another namespace.
PORTABLE_TYPES = ("source", "installers", "models")
# Types whose capture requires stopped services. Models use a consistent snapshot instead.
SERVICE_TYPES = ("webui", "terminal")
TERMINAL_VOLUME = "terminal/users.tar"
TERMINAL_KEY = "terminal/terminal-server.key"
MEMBER_NAMES = frozenset({*(f"{kind}.tar" for kind in TYPES), TERMINAL_VOLUME, TERMINAL_KEY})
CHECKOUT_RESERVED_PATHS = {CHECKSUMS, "backup", "dist", "docker-project", LAUNCHER, ".run.lock"}
CONFIG_SUFFIXES = {".ini", ".model", ".list", ".sources"}
TERMINAL_KEY_STAGING = ".loaf-terminal-key-"
# The core image repositories; the namespace name or latest tags them.
IMAGES = ("loaf/ollama-online", "loaf/ollama-offline", "loaf/open-webui")
TERMINAL_IMAGE = "loaf/open-terminal"
# Compose volume keys and their archive types. The namespace prefixes each volume name.
VOLUMES = {"ollama": "ollama", "open-webui": "webui"}
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_VOLUME_LABEL = "com.docker.compose.volume"
# Match the ownership labels set by the terminal provisioner.
WORKSPACE_PROJECT_LABEL = "loaf.terminal.project"
# The provisioner's user volume holds every account's /home/user folder.
TERMINAL_USERS_LABEL = "loaf.terminal.users"
COPY_BUFFER = 1024 * 1024
# Checks read each member once as a stream. Manifests can follow the files they name, so small files stay in memory.
SMALL_FILE = 1024 * 1024
SMALL_FILES_LIMIT = 256 * 1024 * 1024
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

# Open WebUI keeps each setting as one row of its config table and seeds only missing rows at startup.
# Dropping an emptied row lets a later start with the terminal server enabled seed loaf's connection again.
DROP_TERMINAL_CONNECTION = """\
import json, sqlite3, sys
database = "/app/backend/data/webui.db"
key = "terminal_server.connections"
try:
    db = sqlite3.connect(f"file:{database}?mode=rw", uri=True)
except sqlite3.OperationalError:
    sys.exit(0)
with db:
    try:
        row = db.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    connections = json.loads(row[0]) if row else None
    if isinstance(connections, list):
        kept = [entry for entry in connections if not (isinstance(entry, dict) and entry.get("id") == "loaf-terminal")]
        if kept != connections:
            if kept:
                db.execute("UPDATE config SET value = ? WHERE key = ?", (json.dumps(kept), key))
            else:
                db.execute("DELETE FROM config WHERE key = ?", (key,))
            print("Removed loaf's saved terminal connection from the restored Open WebUI data.")
db.close()
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
    tag = config.namespace_name(namespace) or "latest"
    return tuple(f"{image}:{tag}" for image in (*IMAGES, *((TERMINAL_IMAGE,) if include_terminal else ())))


def volume_names(namespace: str) -> dict[str, str]:
    return {f"{namespace}-{key}": kind for key, kind in VOLUMES.items()}


def ensure_compose_volume(project: Path, namespace: str, key: str) -> None:
    """Create a missing deployment volume with the labels Compose gives its own volumes.

    A volume that a transfer container created implicitly lacks them, and Compose then warns that it already exists.
    """
    name = f"{namespace}-{key}"
    if subprocess.run(["docker", "volume", "inspect", name], cwd=project,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        subprocess.check_output(["docker", "volume", "create", "--label", f"{COMPOSE_PROJECT_LABEL}={namespace}",
                                 "--label", f"{COMPOSE_VOLUME_LABEL}={key}", name], cwd=project)


def expand_types(names, *, terminal_available: bool = True) -> tuple[str, ...]:
    """Resolve archive types and aliases. The terminal type needs the WebUI data that owns its accounts."""
    selected = set()
    for name in names:
        if name in ALIASES:
            selected.update(kind for kind in ALIASES[name] if kind != "terminal" or terminal_available)
        elif name in TYPES:
            selected.add(name)
        else:
            raise TransferError(f"Unknown archive type {name}. Use {', '.join(TYPES)}, project or volumes.")
    if "terminal" in selected:
        selected.add("webui")
    return tuple(kind for kind in TYPES if kind in selected)


def default_backup_types(project: Path, *, terminal: bool) -> tuple[str, ...]:
    """Back up every available type. The source exists only in a checkout; the settings decide the terminal type."""
    checkout = checkout_paths(project) is not None
    return tuple(kind for kind in TYPES if (kind != "source" or checkout) and (kind != "terminal" or terminal))


def backup_path(directory: Path, *, namespace: str = config.DEPLOYMENT) -> Path:
    """A new backup folder: YYYYMMDD-HHMM_local-ai-ferry, with the namespace name before the suffix."""
    name = "_".join(part for part in (f"{datetime.now():%Y%m%d-%H%M}", config.namespace_name(namespace),
                                      BUNDLE_NAME) if part)
    target = directory / name
    sequence = 2
    while target.exists() or target.is_symlink():
        target = directory / f"{name}-{sequence}"
        sequence += 1
    return target


def latest_backup(directory: Path, *, namespace: str = config.DEPLOYMENT) -> Path | None:
    name = config.namespace_name(namespace)
    prefix = re.escape(f"{name}_" if name else "")
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
    def namespace_name(self) -> str:
        # The manifest keeps its original key for the namespace name.
        return self.manifest["deployment"]

    @property
    def skip_terminal_server(self) -> bool:
        return self.manifest.get("skip_terminal_server") is True

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
        raise invalid("missing deployment namespace.")
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
    if "terminal" in kinds and "webui" not in kinds:
        raise invalid("terminal without the WebUI data that owns its accounts.")
    if data.get("skip_terminal_server", True) is not True or ("skip_terminal_server" in data and "terminal" in kinds):
        raise invalid("unexpected skip_terminal_server.")
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
    """Hash what a reader consumes, optionally copy it to an output and report its progress."""

    def __init__(self, stream, progress: Progress | None = None, output=None) -> None:
        self.stream = stream
        self.progress = progress
        self.output = output
        self.digest = sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.digest.update(data)
        if self.output is not None:
            self.output.write(data)
        if self.progress is not None:
            self.progress.advance(len(data))
        return data


def check_member(name: str, stream):
    """Validate one archive member while it is read. Restore and backup --verify run the same checks.

    Returns the image platforms for images.tar and None for the other members.
    """
    try:
        if name == "images.tar":
            return saved_image_tags(stream, name)
        if name == "models.tar":
            validate_model_archive(stream, name)
        elif name == "webui.tar":
            validate_volume_payload(stream, {"webui"}, name)
        elif name == TERMINAL_VOLUME:
            validate_volume_payload(stream, {"users"}, name)
        elif name == "config.tar":
            check_files(stream, "config", name)
        elif name == "installers.tar":
            check_files(stream, INSTALL_CACHE.as_posix(), name)
        elif name == "source.tar":
            check_checkout(stream, name)
        elif name == TERMINAL_KEY and not config.TERMINAL_KEY_PATTERN.fullmatch(stream.read(515)):
            raise TransferError("Invalid terminal server key in the backup.")
    except tarfile.TarError as exc:
        raise TransferError(f"Invalid {name} in the backup: {exc}") from exc
    return None


def unpack(archive: Archive, staging: Path | None, select: set[str]) -> dict:
    """Check every member against the manifest and its content checks, and stage the selected ones.

    Each member is read once: the same bytes are hashed, checked and staged. Nothing changes before
    the whole archive passed. Returns the image platforms by member name.
    """
    expected = {member["path"]: member for item in archive.manifest["items"] for member in item["members"]}
    seen = set()
    results = {}
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
                        reader = HashingReader(payload, progress, output)
                        try:
                            if (result := check_member(member.name, reader)) is not None:
                                results[member.name] = result
                        except TransferError as problem:
                            # A changed member reports the hash mismatch rather than what it broke.
                            while reader.read(COPY_BUFFER):
                                pass
                            if reader.digest.hexdigest() != expected[member.name]["sha256"]:
                                raise TransferError(f"SHA-256 verification failed for {member.name}.") from problem
                            raise
                        # The content check can end before the member does, for example at tar padding.
                        while reader.read(COPY_BUFFER):
                            pass
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
    return results


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


# --- Member checks on streams ---

@contextmanager
def tar_stream(source):
    """Read a tar once from start to end, from a path or a stream, without seeking or temporary files."""
    with ExitStack() as stack:
        stream = stack.enter_context(open(source, "rb")) if isinstance(source, (str, os.PathLike)) else source
        yield stack.enter_context(tarfile.open(fileobj=stream, mode="r|", bufsize=COPY_BUFFER))


def member_label(source, name: str | None) -> str:
    return name or (Path(source).name if isinstance(source, (str, os.PathLike)) else "archive member")


class SmallFiles:
    """The small regular files of a tar read once, for manifests that name files stored before them."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.files: dict[str, bytes] = {}
        self.size = 0

    def add(self, archive: tarfile.TarFile, member: tarfile.TarInfo) -> None:
        if not member.isfile() or member.size > SMALL_FILE:
            return
        self.size += member.size
        if self.size > SMALL_FILES_LIMIT:
            raise TransferError(f"{self.label} holds more small files than loaf checks "
                                f"({SMALL_FILES_LIMIT // 2 ** 20} MiB).")
        with archive.extractfile(member) as payload:
            self.files[posixpath.normpath(member.name)] = payload.read()

    def __getitem__(self, name: str) -> bytes:
        return self.files[posixpath.normpath(name)]


def link_target(path: PurePosixPath, linkname: str) -> PurePosixPath | None:
    """The archive path a symbolic link names, or None when the link is absolute or leaves the archive's root."""
    if not linkname or PurePosixPath(linkname).is_absolute():
        return None
    target = PurePosixPath(posixpath.normpath(posixpath.join(path.parent.as_posix(), linkname)))
    return None if target.parts[:1] == ("..",) else target


def check_file_members(members: Iterable[tarfile.TarInfo], prefix: str, label: str) -> None:
    """Accept regular files below one top-level directory. config.tar may also hold links to its own files."""
    files, links, seen = set(), {}, set()
    for member in members:
        path = PurePosixPath(member.name)
        link = prefix == "config" and member.issym()
        if (not (member.isfile() or link) or not path.is_relative_to(prefix) or ".." in path.parts
                or path.is_absolute() or path in seen or (prefix == "config" and path.suffix not in CONFIG_SUFFIXES)):
            raise TransferError(f"Invalid file in {label}: {member.name!r}")
        seen.add(path)
        if link:
            links[path] = link_target(path, member.linkname)
        else:
            files.add(path)
    for path, target in links.items():
        if target is None or target not in files:
            raise TransferError(f"Invalid link in {label}: {str(path)!r} must point to a configuration file "
                                f"archived in {prefix}/.")


def check_files(source, prefix: str, label: str | None = None) -> None:
    with tar_stream(source) as archive:
        check_file_members(archive, prefix, member_label(source, label))


def check_checkout_members(members: Iterable[tarfile.TarInfo], label: str) -> None:
    """Accept checkout paths and links that stay in the checkout, and require a complete loaf checkout."""
    seen, files = set(), set()
    for member in members:
        path = PurePosixPath(member.name)
        if (not path.parts or path.is_absolute() or ".." in path.parts or path in seen
                or path.parts[0] in CHECKOUT_RESERVED_PATHS or path.parts[0].startswith(".loaf-")
                or not (member.isfile() or member.isdir() or member.issym() or member.islnk())):
            raise TransferError(f"Invalid checkout path in {label}: {member.name!r}")
        if member.issym() and link_target(path, member.linkname) is None:
            raise TransferError(f"Checkout link in {label} leaves the checkout: {member.name!r}")
        if member.islnk() and (PurePosixPath(member.linkname) not in files):
            raise TransferError(f"Invalid hard link in {label}: {member.name!r}")
        seen.add(path)
        if member.isfile():
            files.add(path)
    if PurePosixPath(PYTHON_PACKAGE_DIR, "pyproject.toml") not in files or not any(
            path.parts[0] == "docs" for path in seen):
        raise TransferError("The source type in this backup is not a complete loaf checkout.")


def check_checkout(source, label: str | None = None) -> None:
    with tar_stream(source) as archive:
        check_checkout_members(archive, member_label(source, label))


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


def archive_config(config_dir: Path, destination: Path, *, exclude_file: Path | None) -> None:
    """Archive settings and saved Modelfiles. Links stay links and must name one of the archived files."""
    files = sorted(path for path in config_dir.rglob("*") if path.suffix in CONFIG_SUFFIXES and path.is_file()
                   and path.resolve() != exclude_file) if config_dir.is_dir() else []
    regular = {path for path in files if not path.is_symlink()}
    for path in files:
        if path.is_symlink():
            link = os.readlink(path)
            target = link_target(PurePosixPath(path.relative_to(config_dir).as_posix()), link)
            if target is None or config_dir / target not in regular:
                raise TransferError(
                    f"{path} links to {link}. A backup keeps a link only when it points, relatively, to another "
                    f"configuration file in {config_dir}. Replace it with such a link or a copy.")
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
    # The checked standalone Python runtime downloads that scripts/build-release.sh caches.
    paths.update(path.relative_to(project) for path in (project / PYTHON_RUNTIME_DIR).glob("*.tar.gz")
                 if path.is_file() and not path.is_symlink())
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

    def checked(member):
        if project / member.name == exclude_file:
            return None
        # Restore refuses links that leave the checkout, so the backup refuses them first.
        if member.issym() and link_target(PurePosixPath(member.name), member.linkname) is None:
            raise TransferError(f"{project / member.name} links to {member.linkname}, outside the checkout. "
                                "A source backup keeps only links that stay inside it.")
        return member

    with Progress("Archive source.tar") as progress, destination.open("wb") as output:
        stream = TrackedStream(output, progress)
        with tarfile.open(fileobj=stream, mode="w|", bufsize=COPY_BUFFER, copybufsize=COPY_BUFFER) as archive:
            archive.add(project / ".git", arcname=".git", filter=checked)
            for relative in sorted(paths):
                path = project / relative
                # Staging folders of this and other loaf operations stay out, like the backup folders.
                if (relative.parts[0] in project_parts or any(part.startswith(".loaf-") for part in relative.parts)
                        or path == exclude_file or any(relative.is_relative_to(bundle) for bundle in bundle_dirs)):
                    continue
                if relative == Path(LAUNCHER) and path.is_symlink():
                    continue
                if path.exists() or path.is_symlink():
                    archive.add(path, arcname=relative.as_posix(), recursive=False, filter=checked)


def stage_files(source: Path, target: Path, prefix: str) -> None:
    """Extract the files below one top-level directory, and config.tar's links to its own files."""
    with tarfile.open(source, "r:") as archive:
        members = archive.getmembers()
        check_file_members(members, prefix, source.name)
        for member in members:
            destination = target / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                destination.symlink_to(member.linkname)
                continue
            with destination.open("xb") as output, archive.extractfile(member) as payload:
                shutil.copyfileobj(payload, output, length=COPY_BUFFER)


def project_file_targets(source: Path, target: Path) -> list[tuple[Path, Path]]:
    """Map staged files and links onto a directory. Each replaces the entry at its path, never writes through it."""
    files = []
    for path in sorted(source.rglob("*")) if source.is_dir() else ():
        if path.is_dir() and not path.is_symlink():
            continue
        destination = target / path.relative_to(source)
        if (destination.parent.resolve() != destination.parent
                or (destination.exists() and not (destination.is_file() or destination.is_symlink()))):
            raise TransferError(f"Cannot restore a file to {destination}.")
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
            if source.is_symlink():
                replacement.symlink_to(os.readlink(source))
            else:
                shutil.copyfile(source, replacement)
                if group is not None:
                    os.chown(replacement, -1, group)
                replacement.chmod(0o600 if group is None else 0o640)
            replacement.replace(destination)


def stage_configuration(recovered: Path, lower: tuple[Path, ...], destination: Path, namespace_name: str,
                        terminal_key: Path | None = None, *, disable_terminal: bool = False) -> config.Settings:
    """Load recovered settings over the lower configuration layers and keep the backup's namespace.

    Changes edit single lines, so comments stay. Renamed settings get their current names. A given terminal
    key path stays configured: the system deployment's key does not move with its settings. A backup made
    without the terminal server restores it disabled.
    """
    target = recovered / config.CONFIG_NAME
    source = target if target.is_file() else destination / config.CONFIG_NAME
    text = source.read_text(encoding="utf-8-sig") if source.is_file() else ""

    def write(content: str) -> config.Settings:
        recovered.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        target.write_text(content, encoding="utf-8")
        return config.load(*lower, recovered)

    current = config.edit_text(text, {})
    settings = write(current) if current != text else config.load(*lower, source.parent)
    pinned = {}
    if settings.namespace_name != namespace_name:
        pinned[("launcher", "namespace")] = namespace_name
    if terminal_key is not None and settings.terminal_server_secret_file != terminal_key:
        pinned[("terminal_server", "secret_file")] = str(terminal_key)
    if disable_terminal and settings.terminal_server_enabled:
        pinned[("terminal_server", "enabled")] = "false"
    return write(config.edit_text(current, pinned)) if pinned else settings


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
        check_checkout_members(members, source.name)
        archive.extractall(checkout, members=members, filter=checked_member)


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


def local_image(project: Path, image: str) -> bool:
    return not subprocess.run(["docker", "image", "inspect", image], cwd=project,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def require_helper_image(project: Path, namespace: str) -> None:
    image = image_names(namespace)[0]
    if not local_image(project, image):
        raise TransferError(f"Restoring volumes without images requires the local helper image {image}.")


def existing_volume(project: Path, name: str) -> dict | None:
    listed = subprocess.check_output(["docker", "volume", "ls", "--quiet", "--filter", f"name={name}"], cwd=project)
    if name not in listed.decode().split():
        return None
    volume, = json.loads(subprocess.check_output(["docker", "volume", "inspect", name], cwd=project))
    return volume


def require_empty_volume(project: Path, namespace: str, name: str) -> dict | None:
    """Refuse an existing restore target that is in use or holds data, before the restore changes anything.

    Returns the existing volume's record, or None when restore will create the volume.
    """
    volume = existing_volume(project, name)
    if volume is None:
        return None
    require_inactive_volume(project, name)
    image = image_names(namespace)[0]
    if not local_image(project, image):
        raise TransferError(f"The {name} volume exists. Checking that it is empty needs the local image {image}. "
                            "Restore the images first, then the volumes.")
    entry = subprocess.check_output([
        "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
        "--mount", f"type=volume,source={name},target=/volume,readonly,volume-nocopy",
        "--entrypoint", "find", image, "/volume", "-mindepth", "1", "-print", "-quit",
    ], cwd=project)
    if entry.strip():
        raise TransferError(
            f"Restore needs a new or empty {name} volume, but it holds data. Save it with loaf backup if you need "
            f"it, remove it with 'docker volume rm {name}', or restore into another namespace with --namespace.")
    return volume


def volume_command(*, restore: bool = False, namespace: str = config.DEPLOYMENT,
                   volumes: dict[str, str] | None = None) -> list[str]:
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
    volume = f"{namespace}-ollama"
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


def saved_image_tags(source, label: str | None = None) -> dict[str, set[str]]:
    """Return each image tag in a docker image save archive with its platforms, reading the archive once.

    docker image save writes manifest.json after the blobs it names, so small files stay in memory.
    """
    label = member_label(source, label)
    small = SmallFiles(label)
    with tar_stream(source) as images:
        for member in images:
            small.add(images, member)
    try:
        platforms = {}
        for image in json.loads(small["manifest.json"]):
            platform = json.loads(small[image["Config"]])
            fields = [platform["os"], platform["architecture"]]
            if platform.get("variant"):
                fields.append(platform["variant"])
            for tag in image["RepoTags"] or ():
                platforms.setdefault(tag, set()).add("/".join(fields))
    except (AttributeError, KeyError, ValueError, TypeError) as exc:
        raise TransferError(f"Invalid Docker image manifest in {label}.") from exc
    return platforms


def check_image_tags(platforms: dict[str, set[str]], namespace: str) -> dict[str, set[str]]:
    core = set(image_names(namespace))
    if platforms.keys() not in (core, set(image_names(namespace, include_terminal=True))):
        raise TransferError(
            f"Backup image tags {', '.join(sorted(platforms))} do not match namespace {namespace}. Select the "
            "matching --namespace, or restore the images with the loaf release that created the backup.")
    return platforms


def validate_volume_payload(source, volumes: set[str], label: str | None = None) -> None:
    """Accept only the named volume directories, each present, without links leaving their volume."""
    label = member_label(source, label)
    missing = set(volumes)
    entries, links, targets = set(), set(), set()
    with tar_stream(source) as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if not name.parts or name.parts[0] not in volumes or ".." in name.parts or name in entries:
                raise TransferError(f"Unexpected path in {label}: {member.name!r}")
            if member.ischr() or member.isblk() or (len(name.parts) == 1 and not member.isdir()):
                raise TransferError(f"Unexpected file type in {label}: {member.name!r}")
            entries.add(name)
            if len(name.parts) == 1:
                missing.discard(name.name)
            if member.issym() or member.islnk():
                links.add(name)
            if member.islnk():
                target = PurePosixPath(member.linkname)
                if not target.parts or target.parts[0] != name.parts[0] or ".." in target.parts:
                    raise TransferError(f"Invalid hard link in {label}: {member.name!r}")
                targets.add(target)
    if any(not links.isdisjoint(entry.parents) for entry in entries | targets):
        raise TransferError(f"A path in {label} traverses an archived link.")
    if missing:
        raise TransferError(f"{label} is missing data for {', '.join(sorted(missing))}.")


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


def read_model_manifest(files: SmallFiles, member: tarfile.TarInfo, digest: str | None = None) -> dict:
    if member.size > SMALL_FILE:
        raise ValueError("Model manifest exceeds 1 MiB.")
    data = files[member.name]
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


def validate_model_archive(source, label: str | None = None) -> None:
    """Check a model snapshot in one pass: its paths, and each manifest's blobs with their sizes."""
    small = SmallFiles(member_label(source, label))
    entries = {}
    manifests = []
    with tar_stream(source) as archive:
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
            if name.parts[:2] in (("models", "manifests"), ("models", "manifests-v2"), ("models", "blobs")):
                small.add(archive, member)
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
                definition = read_model_manifest(small, model_blob(entries, digest), digest)
            else:
                definition = read_model_manifest(small, manifest)

            if definition.get("mediaType") == MANIFEST_LIST_TYPE:
                children = definition["manifests"]
                if not isinstance(children, list) or not children:
                    raise ValueError("Expected a non-empty manifest list.")
                for child in children:
                    if not isinstance(child, dict):
                        raise ValueError("Expected a child manifest object.")
                    if "digest" in child:
                        digest = child["digest"]
                        child = read_model_manifest(small, model_blob(entries, digest), digest)
                    validate_model_layers(entries, child)
            else:
                validate_model_layers(entries, definition)
        except (KeyError, TypeError, ValueError) as exc:
            raise TransferError(f"Invalid model manifest {manifest.name!r}. {exc}") from exc


def model_volume_command(project: Path, namespace: str, *, restore: bool = False, delete: bool = False) -> list[str]:
    volume = f"{namespace}-ollama"
    if not restore:
        subprocess.check_output(["docker", "volume", "inspect", volume], cwd=project)
    consumers = subprocess.check_output([
        "docker", "ps", "--filter", f"volume={volume}", "--format",
        '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}',
    ], cwd=project).decode().splitlines()
    serving = {f"{namespace}/ollama-{profile}" for profile in ("connected", "offline")}
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


# --- Terminal user volume: every account's /home/user folder ---

def workspace_prefix(namespace: str) -> str:
    """The provisioner names its containers, networks and user volume with this prefix."""
    return f"{namespace}-terminal-"


def terminal_volume(namespace: str) -> str:
    """The provisioner's user volume. Each chat container mounts only its account's folder of it."""
    return f"{workspace_prefix(namespace)}users"


def terminal_volume_labels(namespace: str) -> dict[str, str]:
    return {WORKSPACE_PROJECT_LABEL: namespace, TERMINAL_USERS_LABEL: "true"}


def terminal_volume_exists(project: Path, namespace: str) -> bool:
    name = terminal_volume(namespace)
    listed = subprocess.check_output(["docker", "volume", "ls", "--quiet", "--filter", f"name={name}"], cwd=project)
    if name not in listed.decode().split():
        return False
    volume, = json.loads(subprocess.check_output(["docker", "volume", "inspect", name], cwd=project))
    if (volume["Labels"] or {}) != terminal_volume_labels(namespace):
        raise TransferError(f"The terminal user volume {name} does not match the deployment's ownership labels.")
    return True


def check_volume_settings(volume: dict, labels: dict[str, str], driver: str, options: dict[str, str]) -> None:
    if (volume["Labels"] or {}) != labels or volume["Driver"] != driver or (volume["Options"] or {}) != options:
        raise TransferError(f"Terminal volume {volume['Name']} does not match the deployment's ownership or "
                            "storage settings.")


def create_labeled_volume(project: Path, name: str, labels: dict[str, str], driver: str, options: dict[str, str]) -> None:
    command = ["docker", "volume", "create", "--driver", driver]
    for label, value in labels.items():
        command += ["--label", f"{label}={value}"]
    for option, value in options.items():
        command += ["--opt", f"{option}={value}"]
    subprocess.check_output([*command, name], cwd=project)
    volume, = json.loads(subprocess.check_output(["docker", "volume", "inspect", name], cwd=project))
    check_volume_settings(volume, labels, driver, options)


def drop_terminal_connection(project: Path, namespace: str, volume: str) -> None:
    """Remove loaf's saved terminal connection from restored Open WebUI data, with the network-less WebUI image."""
    subprocess.run([
        "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--tmpfs", "/tmp",
        "--mount", f"type=volume,source={volume},target=/app/backend/data,volume-nocopy",
        "--entrypoint", "python3", image_names(namespace)[2], "-c", DROP_TERMINAL_CONNECTION,
    ], cwd=project, check=True)


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
class Terminal:
    key: bytes
    # Whether the backup holds the user volume; a deployment without terminal use has none yet.
    users: bool


def read_terminal(item: dict, staging: Path) -> Terminal:
    """Read the terminal type that unpack already checked."""
    return Terminal((staging / TERMINAL_KEY).read_bytes(), TERMINAL_VOLUME in {member["path"] for member in item["members"]})


# --- Backup ---

def check_backup_types(project: Path, config_dir: Path, types: tuple[str, ...], *, terminal_key: Path,
                       skip_terminal_server: bool = False) -> None:
    """Refuse types this deployment cannot back up, before any service stops. Dry runs use the same checks."""
    if not types or set(types) - set(TYPES):
        raise TransferError(f"Select archive types from {', '.join(TYPES)}.")
    if "terminal" in types and "webui" not in types:
        raise TransferError("The terminal type needs the WebUI data that owns its accounts. Add webui.")
    if "terminal" in types and skip_terminal_server:
        raise TransferError("--no-terminal-server leaves out the terminal type. Omit one of them.")
    checkout = checkout_paths(project) is not None if {"source", "config"} & set(types) else False
    if "source" in types and not checkout:
        raise TransferError("The source type needs a Git checkout. Omit source for this deployment.")
    # Settings can come entirely from the system or packaged layers, but the directory must be a deployment.
    if "config" in types and not (config_dir.is_dir() or (project / "docker-project").is_symlink() or checkout):
        raise TransferError("Backup needs a deployment prepared by loaf setup or a Git checkout.")
    if "terminal" in types:
        config.read_terminal_secret(terminal_key)


def backup(
    project: Path,
    destination: Path,
    prepare_services: Callable[[], None],
    *,
    types: tuple[str, ...],
    namespace: str = config.DEPLOYMENT,
    namespace_name: str = "",
    compression_level: int = config.DEFAULT_COMPRESSION_LEVEL,
    terminal_key: Path | None = None,
    skip_terminal_server: bool = False,
    config_dir: Path | None = None,
) -> None:
    """Write one archive: loaf.json first, then the members of each selected type.

    skip_terminal_server leaves out the terminal image and type and records that in loaf.json, so restore
    disables the terminal server.
    """
    if destination.is_relative_to(project / ".git"):
        raise TransferError("Choose a backup directory outside .git.")
    terminal_key = terminal_key or project / config.TERMINAL_SECRET
    config_dir = config_dir or project / "config"
    check_backup_types(project, config_dir, types, terminal_key=terminal_key, skip_terminal_server=skip_terminal_server)
    key = config.read_terminal_secret(terminal_key) if "terminal" in types else None
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
            archive_config(config_dir, staging / "config.tar", exclude_file=terminal_key)
            add("config", "config.tar")
        if "installers" in types:
            print("Collecting cached installers…", file=sys.stderr, flush=True)
            archive_installers(project, staging / "installers.tar", exclude_file=terminal_key)
            add("installers", "installers.tar")
        if "images" in types:
            images = image_names(namespace)
            if not skip_terminal_server:
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
            # A deployment that never started WebUI saves an empty volume.
            ensure_compose_volume(project, namespace, "open-webui")
            docker_archive(project, volume_command(namespace=namespace, volumes={webui: "webui"}),
                           staging / "webui.tar", "Save webui.tar")
            add("webui", "webui.tar")
        if "terminal" in types:
            (staging / "terminal").mkdir()
            members = []
            if terminal_volume_exists(project, namespace):
                users = terminal_volume(namespace)
                require_inactive_volume(project, users)
                docker_archive(project, volume_command(namespace=namespace, volumes={users: "users"}),
                               staging / TERMINAL_VOLUME, f"Save {TERMINAL_VOLUME}")
                members.append(TERMINAL_VOLUME)
            (staging / TERMINAL_KEY).write_bytes(key)
            add("terminal", *members, TERMINAL_KEY)

        manifest = {
            "format": ARCHIVE_FORMAT, "version": ARCHIVE_VERSION, "created": datetime.now().astimezone().isoformat(),
            "loaf": assets.release_version(), "deployment": namespace_name,
            **({"skip_terminal_server": True} if skip_terminal_server else {}), "items": items,
        }
        # Publish only after all payloads are complete, preserving any old backup
        # if a command fails or the operation is interrupted.
        pending = staging / destination.name
        write_archive(staging, pending, manifest, compression_level)
        pending.chmod(file_modes(destination.parent)[0])
        pending.replace(destination)


def backup_bundle(project: Path, destination: Path, prepare_services: Callable[[], None], *,
                  namespace_name: str, executable: Path | None = None, **options) -> None:
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
        backup(project, archive, prepare_services, namespace=config.namespace(namespace_name),
               namespace_name=namespace_name, **options)
        sums = bundle / CHECKSUMS
        sums.write_text(f"{file_digest(copy)}  {LAUNCHER}\n{file_digest(archive)}  {archive.name}\n", encoding="utf-8")
        sums.chmod(private)
        bundle.rename(destination)


# --- Verify and restore ---

def describe(archive: Archive) -> list[str]:
    lines = [f"Archive format {archive.manifest['version']}, created {archive.manifest.get('created', 'unknown')} "
             f"by loaf {archive.manifest.get('loaf', 'unknown')} for "
             f"namespace {config.namespace(archive.namespace_name)}."]
    for item in archive.manifest["items"]:
        size = sum(member["size"] for member in item["members"])
        lines.append(f"  {item['type']:<10} {size:>15,} bytes")
    if archive.skip_terminal_server:
        lines.append("  Made with --no-terminal-server: restore disables the terminal server.")
    return lines


def verify_backup(archive: Archive, namespace: str) -> None:
    """Run restore's archive checks on the stream, without Docker or temporary copies."""
    checked = unpack(archive, None, set())
    platforms = check_image_tags(checked["images.tar"], namespace) if "images.tar" in checked else {}
    print(f"Verified SHA-256 hashes and contents of all members of {archive.path}")
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
        unpack(archive, staging, {TERMINAL_KEY, *(("installers.tar",) if installers else ())})
        if installers:
            stage_files(staging / "installers.tar", staging / "files", INSTALL_CACHE.as_posix())
            publish_project_files(project_file_targets(staging / "files", project))
        key = staging / TERMINAL_KEY
        if key.is_file():
            data = key.read_bytes()
            if not config.TERMINAL_KEY_PATTERN.fullmatch(data):
                raise TransferError("Invalid terminal server key in the backup.")
            return data
    return None


def requested_types(archive: Archive, types) -> tuple[str, ...]:
    """The types a restore applies: the named ones, which the archive must contain, or all of its types."""
    items = archive.items
    requested = expand_types(types) if types else tuple(kind for kind in TYPES if kind in items)
    missing = [kind for kind in requested if kind not in items]
    if missing:
        raise TransferError(f"{archive.path.name} does not contain {', '.join(missing)}.")
    return requested


def restore(project: Path, archive: Archive, types: tuple[str, ...] | None = None, *,
            namespace: str = config.DEPLOYMENT,
            settings: config.Settings | None = None, configuration: tuple[tuple[Path, ...], Path, str] | None = None,
            delete: bool = False, system: bool = False) -> RestoredBackup:
    """Verify the whole archive, then restore the selected types in order.

    The system deployment shares restored settings and installers with its directories' group,
    keeps its terminal key path and lets that group read a restored key. A backup made with
    --no-terminal-server restores the terminal server disabled and without loaf's saved WebUI connection.
    """
    items = archive.items
    requested = requested_types(archive, types)
    settings = settings or config.Settings()
    config_dir = configuration[1] if configuration else project / "config"
    project.mkdir(parents=True, exist_ok=True)
    # Stage files in the project so the archive can stay on read-only media.
    with TemporaryDirectory(prefix=".loaf-restore-", dir=project) as folder:
        staging = Path(folder)
        print(f"Checking and unpacking {archive.path}…", file=sys.stderr, flush=True)
        checked = unpack(archive, staging, {member["path"] for kind in requested for member in items[kind]["members"]})

        # Check everything before the first change. unpack checked each member's content.
        if "images" in requested:
            check_image_tags(checked["images.tar"], namespace)
        terminal = read_terminal(items["terminal"], staging) if "terminal" in requested else None
        checkout = staging / "checkout"
        if "source" in requested:
            stage_checkout(staging / "source.tar", checkout)
        files = staging / "files"
        restored_settings = None
        if "config" in requested:
            stage_files(staging / "config.tar", files, "config")
            if configuration is not None:
                lower, _, namespace_name = configuration
                system_key = config.terminal_secret_path(project, settings.terminal_server_secret_file) if system else None
                settings = restored_settings = stage_configuration(
                    files / "config", lower, config_dir, namespace_name, system_key,
                    disable_terminal=archive.skip_terminal_server)
        if "installers" in requested:
            stage_files(staging / "installers.tar", files, INSTALL_CACHE.as_posix())
        checkout_entries = checkout_targets(checkout, project) if "source" in requested else []
        config_targets = project_file_targets(files / "config", config_dir)
        installer_targets = project_file_targets(files / INSTALL_CACHE, project / INSTALL_CACHE)
        terminal_key = config.terminal_secret_path(project, settings.terminal_server_secret_file)
        # The key keeps its own content and mode; a restored settings file never replaces it.
        if any(path == terminal_key for _, path in config_targets + installer_targets):
            raise TransferError("The backup uses the terminal key path for a configuration file.")
        if terminal is not None:
            matching_terminal_key(terminal_key, terminal.key)
        if {"models", "webui", "terminal"} & set(requested) and "images" not in requested:
            require_helper_image(project, namespace)
        webui = next(name for name, role in volume_names(namespace).items() if role == "webui")
        if "webui" in requested:
            require_empty_volume(project, namespace, webui)
        users = terminal_volume(namespace)
        users_options = json.loads(settings.terminal_server_volume_options)
        if terminal is not None and terminal.users:
            existing = require_empty_volume(project, namespace, users)
            if existing is not None:
                check_volume_settings(existing, terminal_volume_labels(namespace), settings.terminal_server_volume_driver,
                                      users_options)
        drop_connection = archive.skip_terminal_server and "webui" in requested
        webui_image = image_names(namespace)[2]
        if drop_connection and "images" not in requested and not local_image(project, webui_image):
            raise TransferError(f"This backup leaves out the terminal server, so restore removes loaf's saved terminal "
                                f"connection from the WebUI data with the local image {webui_image}, which is missing. "
                                "Restore the images too.")
        model_command = model_volume_command(project, namespace, restore=True, delete=delete) if "models" in requested else None

        publish_checkout(checkout_entries, staging / "replaced")
        publish_project_files(config_targets, config_dir.stat().st_gid if system else None)
        publish_project_files(installer_targets, project.stat().st_gid if system else None)
        if "images" in requested:
            docker_archive(project, ["docker", "image", "load"], staging / "images.tar", "Load images.tar", send=True)
        if model_command is not None:
            ensure_compose_volume(project, namespace, "ollama")
            docker_archive(project, model_command, staging / "models.tar", "Restore models.tar", send=True)
        if "webui" in requested:
            ensure_compose_volume(project, namespace, "open-webui")
            docker_archive(project, volume_command(restore=True, namespace=namespace, volumes={webui: "webui"}),
                           staging / "webui.tar", "Restore webui.tar", send=True)
            if drop_connection:
                drop_terminal_connection(project, namespace, webui)
        if terminal is not None:
            if terminal.users:
                create_labeled_volume(project, users, terminal_volume_labels(namespace),
                                      settings.terminal_server_volume_driver, users_options)
            with restore_terminal_key(terminal_key, terminal.key, group=SYSTEM_GROUP if system else None):
                if terminal.users:
                    docker_archive(project, volume_command(restore=True, namespace=namespace, volumes={users: "users"}),
                                   staging / TERMINAL_VOLUME, f"Restore {TERMINAL_VOLUME}", send=True)
            print(f"Terminal server key available at {terminal_key}.")
        if archive.skip_terminal_server and restored_settings is not None:
            print("The backup leaves out the terminal server. The restored settings disable it.", flush=True)
        return RestoredBackup(requested, restored_settings)
