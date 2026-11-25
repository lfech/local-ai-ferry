"""Transfer built images, named volumes and the project in one archive."""

from __future__ import annotations

from configparser import ConfigParser
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
from tempfile import NamedTemporaryFile, TemporaryDirectory, TemporaryFile
from typing import Callable

from . import config
from .progress import Progress, TrackedStream
from .setup import (
    INSTALL_CACHE, PYTHON_PACKAGE_DIR, PYTHON_WHEEL_DIR, TOOLKIT_DIR, WHEEL_CACHE_RECORD,
    docker_installer_files, file_digest, install_backup_host_packages, new_terminal_key, prepare_python_wheels,
    toolkit_files,
)


ARCHIVE_NAME = "local-ai-ferry.tar.gz"
BUNDLE_NAME = "local-ai-ferry"
BUNDLE_MANIFEST = "backup.json"
RESTORED_BACKUP_DIR = "backup"
RESTORED_LAUNCHER = Path("dist/loaf")
CHECKOUT_RESERVED_PATHS = {RESTORED_BACKUP_DIR, BUNDLE_MANIFEST, "dist", "docker-project", "loaf", ".run.lock"}
EXECUTABLE_HEADER = b"#!/bin/bash\n# loaf standalone backup launcher v1\n"
CONFIG_SUFFIXES = {".ini", ".model", ".list", ".sources"}
MODEL_ARCHIVE_NAME = "loaf-volumes.tar.gz"
COMPONENTS = ("volumes", "images", "project")
MEMBERS = tuple(f"{component}.tar" for component in COMPONENTS)
CHECKSUMS = "sha256.json"
TERMINAL_METADATA = "terminal.json"
PAYLOADS = (*MEMBERS, TERMINAL_METADATA)
TERMINAL_KEY_STAGING = ".loaf-terminal-key-"
IMAGES = ("local/ollama-online:latest", "local/ollama-offline:latest", "local/open-webui:latest")
VOLUMES = {"ollama": "ollama", "open-webui": "webui"}
# Match the ownership labels set by the terminal provisioner.
WORKSPACE_PROJECT_LABEL = "loaf.tool-server.project"
WORKSPACE_OWNER_LABEL = "loaf.tool-server.workspace"
COPY_BUFFER = 1024 * 1024
MANIFEST_LIST_TYPE = "application/vnd.ollama.manifest.list.v2+json"

# Check every mount before extracting. On a failed extract,
# clear only the mounts that this restore found empty.
RESTORE_VOLUMES = """\
set -eu
set -- /transfer/ollama /transfer/webui "$@"
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


def archive_name(namespace: str) -> str:
    return f"{namespace}_{ARCHIVE_NAME}" if namespace else ARCHIVE_NAME


def backup_path(directory: Path, *, namespace: str = "", models_only: bool = False) -> Path:
    timestamp = f"{datetime.now():%Y%m%d-%H%M}"
    if models_only:
        return directory / f"{timestamp}_{MODEL_ARCHIVE_NAME}"
    name = "_".join(part for part in (timestamp, namespace, BUNDLE_NAME) if part)
    target = directory / name
    sequence = 2
    while target.exists() or target.is_symlink() or target.with_suffix(".tar.gz").is_file():
        target = directory / f"{name}-{sequence}"
        sequence += 1
    return target


def latest_backup(directory: Path, *, namespace: str = "", models_only: bool = False) -> Path | None:
    name = MODEL_ARCHIVE_NAME if models_only else archive_name(namespace)
    stem = name.removesuffix(".tar.gz")
    suffix = "" if models_only else r"(?:-([0-9]+))?"
    timestamped = re.compile(r"[0-9]{8}-[0-9]{4}_" + re.escape(stem) + suffix + r"\.tar\.gz")
    archives = []
    if models_only or (directory / BUNDLE_MANIFEST).is_file():
        for path in directory.glob(f"*_{stem}*.tar.gz"):
            if path.is_file() and (match := timestamped.fullmatch(path.name)):
                sequence = match[1] if match.lastindex else None
                archives.append((path.name[:13], int(sequence or "0"), path))
    if not models_only:
        prefix = re.escape(f"{namespace}_" if namespace else "")
        folders = re.compile(r"([0-9]{8}-[0-9]{4})_" + prefix + re.escape(BUNDLE_NAME) + r"(?:-([0-9]+))?")
        for path in directory.iterdir() if directory.is_dir() else ():
            if path.is_dir() and (match := folders.fullmatch(path.name)):
                archives.append((match[1], int(match[2] or "1"), path))
    return max(archives)[2] if archives else None


@dataclass(frozen=True)
class BackupBundle:
    archive: Path
    version: str
    executable: Path


@dataclass(frozen=True)
class RestoredBackup:
    components: frozenset[str]
    settings: config.Settings | None


def is_bundle(directory: Path) -> bool:
    return directory.is_dir() and (
        (directory / BUNDLE_MANIFEST).exists()
        or (directory / BUNDLE_MANIFEST).is_symlink()
        or (not (directory / RESTORED_BACKUP_DIR / BUNDLE_MANIFEST).is_file()
            and re.fullmatch(r"[0-9]{8}-[0-9]{4}_(?:.+_)?" + re.escape(BUNDLE_NAME) + r"(?:-[0-9]+)?", directory.name))
    )


def find_bundle(project: Path, selection: Path | None) -> Path | None:
    if selection is not None:
        if is_bundle(selection):
            return selection
        return selection.parent if not selection.is_dir() and is_bundle(selection.parent) else None
    if is_bundle(project):
        return project
    executable = os.environ.get("LOAF_RELEASE_EXECUTABLE")
    if executable and is_bundle(directory := Path(executable).parent):
        return directory
    return None


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


def read_bundle(directory: Path, *, verify: bool = True) -> BackupBundle:
    manifest = directory / BUNDLE_MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise TransferError(f"Backup manifest not found: {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        name, digest, version = (data[key] for key in ("archive", "loaf_sha256", "version"))
        if not isinstance(name, str) or not name.endswith(".tar.gz") or Path(name).name != name:
            raise ValueError("Invalid archive file name.")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid executable hash.")
        if not isinstance(version, str):
            raise ValueError("Invalid deployment version.")
        config.namespace(version)
    except (KeyError, TypeError, ValueError) as exc:
        raise TransferError(f"Invalid backup manifest: {manifest}") from exc
    archive = directory / name
    executable = directory / "loaf"
    if archive.is_symlink() or not archive.is_file():
        raise TransferError(f"Backup archive not found: {archive}")
    if executable.is_symlink() or not standalone_executable(executable):
        raise TransferError(f"Standalone backup executable missing or invalid: {executable}")
    if verify and file_digest(executable) != digest:
        raise TransferError(f"SHA-256 verification failed for {executable}.")
    return BackupBundle(archive, version, executable)


def backup_bundle(project: Path, destination: Path, prepare_volumes: Callable[[], None], *,
                  version: str, executable: Path | None = None, **options) -> None:
    if destination.is_relative_to(project / ".git"):
        raise TransferError("Choose a backup directory outside .git.")
    if executable is None:
        executable = release_executable(project)
    elif not standalone_executable(executable):
        raise TransferError(f"The build did not produce a standalone executable at {executable}.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".loaf-backup-", dir=destination.parent) as folder:
        bundle = Path(folder) / destination.name
        bundle.mkdir(mode=0o700)
        copy = bundle / "loaf"
        shutil.copyfile(executable, copy)
        copy.chmod(0o700)
        archive = bundle / f"{destination.name}.tar.gz"
        manifest = {"archive": archive.name, "loaf_sha256": file_digest(copy), "version": version}
        (bundle / BUNDLE_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        backup(project, archive, prepare_volumes, namespace=config.namespace(version), **options)
        bundle.rename(destination)


def archive_project(project: Path, destination: Path, *, exclude_file: Path | None = None,
                    config_dir: Path | None = None) -> None:
    git_dir = project / ".git"
    has_checkout = git_dir.is_dir() and not git_dir.is_symlink()
    if not has_checkout and not (project / "docker-project/compose.yml").is_file():
        raise TransferError("Backup needs a Git checkout or a deployment prepared by standalone loaf setup.")
    paths = set()
    if has_checkout:
        # Scope Git trust to this checkout and avoid repository fsmonitor hooks.
        files = subprocess.check_output(
            ["git", "-c", f"safe.directory={project.resolve().as_posix()}",
             "-c", "core.fsmonitor=false", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=project,
        )
        paths = {Path(os.fsdecode(name)) for name in files.split(b"\0") if name}
        bundle_dirs = {
            path.parent for path in paths
            if path.name == BUNDLE_MANIFEST and path.parent != Path(".") and (project / path.parent / "loaf").is_file()
        }
        paths = {path for path in paths if not any(path.is_relative_to(bundle) for bundle in bundle_dirs)}
    config_dir = config_dir or project / "config"
    paths = {path for path in paths if not path.is_relative_to(INSTALL_CACHE)}
    environment = project / ".venv"
    if environment.is_symlink():
        raise TransferError("Backup needs .venv to be a local directory.")
    packages = toolkit_files(project)
    wheels = project / PYTHON_WHEEL_DIR
    if (project / PYTHON_PACKAGE_DIR / "pyproject.toml").is_file():
        prepare_python_wheels(project)
        record = wheels / WHEEL_CACHE_RECORD
        if record.is_file() and not record.is_symlink():
            paths.add(record.relative_to(project))
    # Include the prepared installer files from the otherwise ignored cache.
    paths.update(path.relative_to(project) for path in wheels.glob("*.whl") if path.is_file() and not path.is_symlink())
    paths.update(path.relative_to(project) for path in docker_installer_files(project))

    def exclude_secret(member):
        return None if project / member.name == exclude_file else member

    with Progress("Archive project.tar") as progress, destination.open("wb") as output:
        stream = TrackedStream(output, progress)
        with tarfile.open(fileobj=stream, mode="w|", bufsize=COPY_BUFFER, copybufsize=COPY_BUFFER) as archive:
            if has_checkout:
                archive.add(git_dir, arcname=".git", filter=exclude_secret)
            if not has_checkout or config_dir != project / "config":
                paths = {path for path in paths if not path.is_relative_to("config")}
                for path in sorted(config_dir.rglob("*")):
                    if path.suffix in CONFIG_SUFFIXES and path.is_file() and path.resolve() != exclude_file:
                        archive.add(path, arcname=(Path("config") / path.relative_to(config_dir)).as_posix())
            if environment.is_dir():
                archive.add(environment, arcname=".venv", filter=exclude_secret)
            for package in packages:
                if package.resolve() != exclude_file:
                    archive.add(package.resolve(), arcname=(TOOLKIT_DIR / package.name).as_posix())
            for relative in sorted(paths):
                if (
                    relative.name.startswith(TERMINAL_KEY_STAGING)
                    or relative.parts[0] == ".venv"
                ):
                    continue
                path = project / relative
                if path == exclude_file:
                    continue
                if relative == Path("loaf") and path.is_symlink() and path.readlink() == RESTORED_LAUNCHER:
                    continue
                if path.exists() or path.is_symlink():
                    archive.add(path, arcname=relative.as_posix(), recursive=False)


def require_inactive_volumes(project: Path, namespace: str, workspaces: tuple[str, ...] = ()) -> None:
    for volume in (*volume_names(namespace), *workspaces):
        require_inactive_volume(project, volume)


def require_inactive_volume(project: Path, volume: str) -> None:
    consumers = subprocess.check_output(
        ["docker", "ps", "--quiet", "--filter", f"volume={volume}"], cwd=project,
    )
    if consumers.strip():
        raise TransferError(f"The {volume} volume is in use. Stop its containers before transferring it.")


def volume_command(*, restore: bool = False, namespace: str = "", workspaces: tuple[str, ...] = ()) -> list[str]:
    command = ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only"]
    volumes = {**volume_names(namespace), **{name: name for name in workspaces}}
    image = image_names(namespace)[0]
    if restore:
        command.append("-i")
    for volume, directory in volumes.items():
        mount = f"type=volume,source={volume},target=/transfer/{directory},volume-nocopy"
        command += ["--mount", mount if restore else mount + ",readonly"]
    if restore:
        return command + ["--entrypoint", "/bin/sh", image, "-c", RESTORE_VOLUMES,
                          "loaf-restore-volumes", *(f"/transfer/{name}" for name in workspaces)]
    return command + ["--entrypoint", "tar", image, "-cf", "-", "-C", "/transfer", *volumes.values()]


def copy_payload(read: Callable[[int], bytes], destination, progress: Progress) -> None:
    while block := read(COPY_BUFFER):
        if destination is not None:
            destination.write(block)
        progress.advance(len(block))


class HashingReader(TrackedStream):
    def __init__(self, stream, progress: Progress) -> None:
        super().__init__(stream, progress)
        self.digest = sha256()

    def read(self, size: int) -> bytes:
        data = super().read(size)
        self.digest.update(data)
        return data


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


def check_backup_modelfiles(project: Path, volumes: Path, staging: Path, namespace: str) -> list[str]:
    prefix = PurePosixPath("ollama/config")
    blobs = PurePosixPath("/ollama/models/blobs")
    problems = []
    with tarfile.open(volumes, "r:") as snapshot:
        entries = {}
        for member in snapshot:
            path = PurePosixPath(member.name)
            if path.is_relative_to("ollama"):
                entries[path] = member
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
                    blob = entries.get(target.relative_to("/"))
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
    return problems


def workspace_prefix(namespace: str) -> str:
    project = sha256((namespace or "local-inference").encode()).hexdigest()[:16]
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
        WORKSPACE_PROJECT_LABEL: namespace or "local-inference",
        WORKSPACE_OWNER_LABEL: name.removeprefix(workspace_prefix(namespace)),
    }


def list_terminal_workspaces(project: Path, namespace: str) -> tuple[str, ...]:
    label = f"{WORKSPACE_PROJECT_LABEL}={namespace or 'local-inference'}"
    names = subprocess.check_output(["docker", "volume", "ls", "--quiet", "--filter", f"label={label}"], cwd=project)
    workspaces = validate_workspace_names(names.decode().split(), namespace)
    if workspaces:
        volumes = json.loads(subprocess.check_output(["docker", "volume", "inspect", *workspaces], cwd=project))
        if {volume["Name"]: volume["Labels"] for volume in volumes} != {
            name: workspace_labels(namespace, name) for name in workspaces
        }:
            raise TransferError("Terminal workspace volumes do not match the deployment's ownership labels.")
    return workspaces


def read_terminal_backup(staging: Path, namespace: str) -> tuple[bytes, tuple[str, ...]] | None:
    path = staging / TERMINAL_METADATA
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_bytes())
        key = data["key"].encode("ascii")
        names = data["workspaces"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise TransferError("Invalid terminal server backup metadata.") from None
    if not config.TERMINAL_KEY_PATTERN.fullmatch(key):
        raise TransferError("Invalid terminal server key in the backup.")
    return key, validate_workspace_names(names, namespace)


def create_workspace_volumes(project: Path, namespace: str, workspaces: tuple[str, ...],
                             driver: str, options: dict[str, str]) -> None:
    for name in workspaces:
        labels = workspace_labels(namespace, name)
        command = ["docker", "volume", "create", "--driver", driver]
        for label, value in labels.items():
            command += ["--label", f"{label}={value}"]
        for option, value in options.items():
            command += ["--opt", f"{option}={value}"]
        subprocess.check_output([*command, name], cwd=project)
        volume, = json.loads(subprocess.check_output(["docker", "volume", "inspect", name], cwd=project))
        if volume["Labels"] != labels or volume["Driver"] != driver or (volume["Options"] or {}) != options:
            raise TransferError(f"Terminal workspace volume {name} does not match the deployment's ownership or storage settings.")


def matching_terminal_key(path: Path, key: bytes) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    existing = config.read_terminal_secret(path)
    if existing.rstrip(b"\r\n") != key.rstrip(b"\r\n"):
        raise TransferError(f"Terminal server key at {path} differs from the backup.")
    return True


@contextmanager
def restore_terminal_key(path: Path, key: bytes):
    if matching_terminal_key(path, key):
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=TERMINAL_KEY_STAGING) as pending:
        pending.write(key)
        pending.flush()
        try:
            # Publish a complete owner-only file without replacing an existing credential.
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


def validate_volume_payload(path: Path, workspaces: tuple[str, ...]) -> None:
    allowed = {*VOLUMES.values(), *workspaces}
    missing = set(workspaces)
    entries = set()
    links = set()
    targets = set()
    with tarfile.open(path, "r:") as volumes:
        for member in volumes:
            name = PurePosixPath(member.name)
            if not name.parts or name.parts[0] not in allowed or ".." in name.parts or name in entries:
                raise TransferError(f"Unexpected path in the volume backup: {member.name!r}")
            if member.ischr() or member.isblk() or (len(name.parts) == 1 and not member.isdir()):
                raise TransferError(f"Unexpected file type in the volume backup: {member.name!r}")
            entries.add(name)
            if len(name.parts) == 1:
                missing.discard(name.name)
            if member.issym() or member.islnk():
                links.add(name)
            if member.islnk():
                target = PurePosixPath(member.linkname)
                if not target.parts or target.parts[0] != name.parts[0] or ".." in target.parts:
                    raise TransferError(f"Invalid hard link in the volume backup: {member.name!r}")
                targets.add(target)
    if any(not links.isdisjoint(path.parents) for path in entries | targets):
        raise TransferError("A volume backup path traverses an archived link.")
    if missing:
        raise TransferError("The volume backup is missing terminal workspace data.")


def backup(
    project: Path,
    destination: Path,
    prepare_volumes: Callable[[], None],
    *,
    components: tuple[str, ...] = COMPONENTS,
    namespace: str = "",
    compression_level: int = config.DEFAULT_COMPRESSION_LEVEL,
    exclude_project_file: Path | None = None,
    include_terminal: bool = False,
    include_terminal_image: bool = True,
    config_dir: Path | None = None,
) -> None:
    if destination.is_relative_to(project / ".git"):
        raise TransferError("Choose a backup directory outside .git.")
    members = tuple(f"{component}.tar" for component in COMPONENTS if component in components)
    if not members or set(components) - set(COMPONENTS):
        raise TransferError("Select backup components from images, volumes and project.")
    include_terminal = include_terminal and "volumes" in components
    exclude_project_file = exclude_project_file or project / config.TERMINAL_SECRET
    if include_terminal:
        key = config.read_terminal_secret(exclude_project_file)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".loaf-backup-", dir=destination.parent) as folder:
        staging = Path(folder)
        if "project" in components:
            print("Collecting project files…", file=sys.stderr, flush=True)
            archive_project(project, staging / "project.tar", exclude_file=exclude_project_file, config_dir=config_dir)
        if "images" in components:
            images = image_names(namespace)
            if include_terminal_image:
                terminal_image = image_names(namespace, include_terminal=True)[-1]
                if subprocess.check_output(
                    ["docker", "image", "ls", "--quiet", "--filter", f"reference={terminal_image}"], cwd=project,
                ).strip():
                    images += (terminal_image,)
            docker_archive(
                project, ["docker", "image", "save", *images], staging / "images.tar", "Save images.tar",
            )
        if "volumes" in components:
            print("Preparing services for volume backup…", file=sys.stderr, flush=True)
            prepare_volumes()
            workspaces = list_terminal_workspaces(project, namespace) if include_terminal else ()
            require_inactive_volumes(project, namespace, workspaces)
            volumes = staging / "volumes.tar"
            docker_archive(project, volume_command(namespace=namespace, workspaces=workspaces), volumes, "Save volumes.tar")
            for problem in check_backup_modelfiles(project, volumes, staging, namespace):
                print(f"Warning: {problem}", file=sys.stderr, flush=True)

        # Publish only after all payloads are complete, preserving any old backup
        # if a command fails or the operation is interrupted.
        if include_terminal:
            (staging / TERMINAL_METADATA).write_text(
                json.dumps({"key": key.decode("ascii"), "workspaces": workspaces}) + "\n", encoding="utf-8",
            )
            members += (TERMINAL_METADATA,)
        pending = staging / ARCHIVE_NAME
        with tarfile.open(pending, "w:gz", compresslevel=compression_level, copybufsize=COPY_BUFFER) as archive:
            checksums = {}
            for name in members:
                entry = archive.gettarinfo(staging / name, arcname=name)
                with Progress(f"Compress {name}", entry.size) as progress, (staging / name).open("rb") as payload:
                    reader = HashingReader(payload, progress)
                    archive.addfile(entry, reader)
                    checksums[name] = reader.digest.hexdigest()
            metadata = staging / CHECKSUMS
            metadata.write_text(json.dumps(checksums, indent=2) + "\n", encoding="utf-8")
            archive.add(metadata, arcname=CHECKSUMS)
        pending.chmod(0o600)
        pending.replace(destination)


def unpack_payloads(source: Path, staging: Path, *, save: tuple[str, ...]) -> frozenset[str]:
    if not source.is_file():
        raise TransferError(f"Backup archive not found: {source}. Use --backup-dir or --archive to locate it.")
    checksums = {}
    expected = None
    with gzip.open(source, "rb") as compressed:
        with tarfile.open(fileobj=compressed, mode="r|") as archive:
            for member in archive:
                if not member.isfile() or member.name in checksums:
                    raise TransferError(f"Unexpected member in {source.name}: {member.name!r}")
                if member.name == CHECKSUMS and expected is None and member.size <= 4096:
                    try:
                        with archive.extractfile(member) as payload:
                            expected = json.load(payload)
                    except ValueError as exc:
                        raise TransferError(f"Invalid {CHECKSUMS} in {source.name}.") from exc
                    if not isinstance(expected, dict):
                        raise TransferError(f"Invalid {CHECKSUMS} in {source.name}.")
                    continue
                if member.name not in PAYLOADS:
                    raise TransferError(f"Unexpected member in {source.name}: {member.name!r}")
                stage = member.name in save
                target = (staging / member.name).open("wb") if stage else nullcontext()
                with (
                    target as output,
                    Progress(f"{'Unpack' if stage else 'Read'} {member.name}", member.size) as progress,
                    archive.extractfile(member) as payload,
                ):
                    reader = HashingReader(payload, progress)
                    while block := reader.read(COPY_BUFFER):
                        if output is not None:
                            output.write(block)
                    checksums[member.name] = reader.digest.hexdigest()
        # Read to EOF so gzip checks its checksum before Docker state can change.
        while compressed.read(COPY_BUFFER):
            pass
    if expected is None:
        raise TransferError(f"{source.name} has no SHA-256 manifest. Create a new backup to record payload hashes.")
    if expected != checksums:
        mismatches = [name for name in checksums if expected.get(name) != checksums[name]]
        detail = ", ".join(mismatches) or CHECKSUMS
        raise TransferError(f"SHA-256 verification failed for {detail}.")
    components = frozenset(component for component in COMPONENTS if f"{component}.tar" in checksums)
    if TERMINAL_METADATA in checksums and "volumes" not in components:
        raise TransferError(f"{source.name} contains terminal metadata without the volumes component.")
    if not components:
        raise TransferError(f"{source.name} contains no backup components.")
    return components


def image_platforms(path: Path, namespace: str) -> dict[str, set[str]]:
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
                name = "/".join(fields)
                for tag in image["RepoTags"] or ():
                    platforms.setdefault(tag, set()).add(name)
    except (AttributeError, KeyError, ValueError, TypeError) as exc:
        raise TransferError("Invalid Docker image manifest in images.tar.") from exc
    core = set(image_names(namespace))
    with_terminal = set(image_names(namespace, include_terminal=True))
    if platforms.keys() not in (core, with_terminal):
        selection = namespace or "the default namespace"
        raise TransferError(f"Backup image tags do not match {selection}. Select the matching --version.")
    return platforms


def verify_backup(source: Path, namespace: str) -> None:
    with TemporaryDirectory(prefix="loaf-verify-") as folder:
        staging = Path(folder)
        components = unpack_payloads(source, staging, save=("images.tar", TERMINAL_METADATA))
        read_terminal_backup(staging, namespace)
        platforms = image_platforms(staging / "images.tar", namespace) if "images" in components else {}
    print(f"Verified SHA-256 hashes for all payloads in {source}")
    print(f"Components: {', '.join(part for part in COMPONENTS if part in components)}")
    for image, supported in sorted(platforms.items()):
        print(f"  {image}  {', '.join(sorted(supported))}")


def stage_project_files(source: Path, target: Path, *, installers: bool = False) -> None:
    """Stage only the files a standalone deployment can use."""
    with tarfile.open(source, "r:") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if installers:
                selected = path.is_relative_to(INSTALL_CACHE / "deb") and (
                    path.suffix == ".deb" or path.name == "Packages"
                )
            else:
                selected = path.is_relative_to("config") and path.suffix in CONFIG_SUFFIXES
            if not selected:
                continue
            if ".." in path.parts or not member.isfile():
                raise TransferError(f"Invalid project file in backup: {member.name}")
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output, archive.extractfile(member) as payload:
                shutil.copyfileobj(payload, output, length=COPY_BUFFER)


def project_file_targets(source: Path, target: Path) -> list[tuple[Path, Path]]:
    files = []
    for path in sorted(source.rglob("*")):
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


def publish_project_files(files: list[tuple[Path, Path]]) -> None:
    for source, destination in files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=destination.parent, prefix=".loaf-restore-") as directory:
            replacement = Path(directory) / destination.name
            shutil.copyfile(source, replacement)
            replacement.chmod(0o600)
            replacement.replace(destination)


def prepare_bundle_setup(bundle: BackupBundle, project: Path, *, installers: bool) -> bytes | None:
    """Recover the setup credential and optionally the cached Debian packages."""
    with TemporaryDirectory(prefix=".loaf-restore-", dir=project) as folder:
        staging = Path(folder)
        save = (TERMINAL_METADATA, "project.tar") if installers else (TERMINAL_METADATA,)
        components = unpack_payloads(bundle.archive, staging, save=save)
        terminal = read_terminal_backup(staging, config.namespace(bundle.version))
        if installers:
            if "project" not in components:
                raise TransferError("The backup has no project component containing cached host installers.")
            files = staging / "files"
            stage_project_files(staging / "project.tar", files, installers=True)
            publish_project_files(project_file_targets(files, project))
        return terminal[0] if terminal is not None else None


def stage_configuration(recovered: Path, defaults: Path, destination: Path, version: str) -> config.Settings:
    target = recovered / config.CONFIG_NAME
    current = recovered if target.is_file() else destination
    settings = config.load(defaults, current)
    if settings.version == version:
        return settings
    parser = ConfigParser(interpolation=None)
    parser.read(current / config.CONFIG_NAME, encoding="utf-8-sig")
    if not parser.has_section("launcher"):
        parser.add_section("launcher")
    parser.set("launcher", "version", version)
    recovered.mkdir(exist_ok=True)
    with target.open("w", encoding="utf-8") as output:
        parser.write(output)
    return config.load(defaults, recovered)


def stage_checkout(source: Path, checkout: Path) -> None:
    """Extract source files into a private tree and refuse writes through archive links."""
    data_filter = getattr(tarfile, "data_filter", None)
    if data_filter is None:
        raise TransferError("Full project recovery requires the standalone loaf executable or a Python with tar extraction filters.")

    def checked_member(member, directory):
        path = PurePosixPath(member.name)
        target = checkout / path
        if target.is_symlink() or target.resolve() != target:
            raise TransferError(f"Checkout path goes through a link: {member.name!r}")
        if (member.issym() and path.parent == PurePosixPath(".venv/bin")
                and re.fullmatch(r"python[0-9.]*", path.name)
                and re.fullmatch(r"(?:/.+/bin/)?python[0-9.]*", member.linkname)):
            # venv records the original host interpreter. Preserve the link as
            # metadata without using it as an extraction destination.
            checked = data_filter(member.replace(linkname="."), directory).replace(linkname=member.linkname)
        else:
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
        raise TransferError("This backup has no complete source checkout. Use loaf setup followed by loaf restore.")


def checkout_targets(checkout: Path, project: Path) -> list[tuple[Path, Path]]:
    def collect(source: Path, target: Path, *, merge: bool):
        if not target.exists() and not target.is_symlink():
            yield source, target
        elif merge and not (source.is_symlink() or target.is_symlink()):
            if source.is_dir() and target.is_dir():
                for entry in source.iterdir():
                    yield from collect(entry, target / entry.name, merge=True)
            elif source.is_file() and target.is_file():
                yield source, target
            else:
                raise TransferError(f"Cannot restore checkout over {target}.")
        else:
            raise TransferError(f"Cannot restore checkout over {target}.")

    targets = []
    for entry in checkout.iterdir():
        targets.extend(collect(entry, project / entry.name, merge=entry.name in {"config", INSTALL_CACHE.name}))
    return targets


def stage_backup_layout(bundle: BackupBundle, project: Path, staging: Path) -> None:
    backup = project / RESTORED_BACKUP_DIR
    dist = project / "dist"
    if backup.is_symlink() or (backup.exists() and (not backup.is_dir() or any(backup.iterdir()))):
        raise TransferError(f"Cannot move the backup bundle to {backup}: use an empty local directory.")
    if dist.is_symlink() or (dist.exists() and not dist.is_dir()):
        raise TransferError(f"Cannot use {dist} for the restored executable.")
    if (dist / "loaf").exists() or (dist / "loaf").is_symlink():
        raise TransferError(f"Cannot replace the existing executable at {dist / 'loaf'}.")

    saved = staging / RESTORED_BACKUP_DIR
    saved.mkdir(mode=0o700)
    for source in (bundle.archive, project / BUNDLE_MANIFEST):
        os.link(source, saved / source.name)
    shutil.copyfile(bundle.executable, saved / "loaf")
    (saved / "loaf").chmod(0o700)
    shutil.copyfile(bundle.executable, staging / "loaf-binary")
    (staging / "loaf-binary").chmod(0o700)
    (staging / "loaf-link").symlink_to(RESTORED_LAUNCHER)


def publish_backup_layout(bundle: BackupBundle, project: Path, staging: Path,
                          checkout_entries: list[tuple[Path, Path]]) -> None:
    binary = project / RESTORED_LAUNCHER
    backup = project / RESTORED_BACKUP_DIR
    previous = staging / "previous"
    previous.mkdir()
    with ExitStack() as rollback:
        def move(source: Path, target: Path) -> None:
            source.rename(target)
            rollback.callback(target.rename, source)

        for index, (entry, destination) in enumerate(checkout_entries):
            if destination.exists():
                move(destination, previous / f"checkout-{index}")
            move(entry, destination)
        if not binary.parent.exists():
            binary.parent.mkdir()
            rollback.callback(binary.parent.rmdir)
        move(staging / "loaf-binary", binary)
        if backup.is_dir():
            move(backup, previous / "empty-backup")
        move(staging / RESTORED_BACKUP_DIR, backup)
        move(project / BUNDLE_MANIFEST, previous / BUNDLE_MANIFEST)
        move(bundle.archive, previous / bundle.archive.name)
        move(bundle.executable, previous / "loaf")
        move(staging / "loaf-link", bundle.executable)
        rollback.pop_all()
    print(f"Backup bundle moved to {backup}.")


def restore(project: Path, source: Path, *, namespace: str = "", terminal_key: Path | None = None,
            workspace_driver: str = "local", workspace_options: str = "{}",
            configuration: tuple[Path, Path, str] | None = None, in_place_bundle: BackupBundle | None = None,
            full_checkout: bool = False, with_docker_deps: bool = False,
            components: tuple[str, ...] | None = None, require_volumes: bool = False) -> RestoredBackup:
    # Stage files in the project so the source can be on read-only media.
    with TemporaryDirectory(prefix=".loaf-restore-", dir=project) as folder:
        staging = Path(folder)
        print(f"Checking and unpacking {source}…", file=sys.stderr, flush=True)
        chosen = components if components is not None else COMPONENTS
        save = tuple(f"{part}.tar" for part in chosen if part != "project" or configuration is not None)
        if "volumes" in chosen:
            save += (TERMINAL_METADATA,)
        available = unpack_payloads(source, staging, save=save)
        missing = set(components or ()) - available
        if missing:
            raise TransferError(f"{source.name} does not contain the requested components: {', '.join(sorted(missing))}.")
        restoring = available.intersection(chosen)
        if full_checkout and "project" not in restoring:
            raise TransferError("Setup --all requires the project component from a source checkout backup.")
        if require_volumes and "volumes" not in restoring:
            raise TransferError("Restore --import requires the volumes component.")
        images = staging / "images.tar"
        terminal = read_terminal_backup(staging, namespace)
        if "images" in restoring:
            image_platforms(images, namespace)
        settings = None
        files = []
        checkout = staging / "checkout"
        project_files = checkout if full_checkout else staging
        if "project" in restoring and configuration is not None:
            defaults, destination, version = configuration
            if full_checkout:
                stage_checkout(staging / "project.tar", checkout)
                if (checkout / "config").is_symlink() or (checkout / "config" / config.CONFIG_NAME).is_symlink():
                    raise TransferError("The checkout configuration must use local files.")
            else:
                stage_project_files(staging / "project.tar", staging)
            settings = stage_configuration(project_files / "config", defaults, destination, version)
            if not full_checkout:
                files = project_file_targets(staging / "config", destination)
            terminal_key = config.terminal_secret_path(project, settings.terminal_server_secret_file)
            workspace_driver = settings.terminal_server_volume_driver
            workspace_options = settings.terminal_server_volume_options
        workspaces = ()
        credential = nullcontext()
        if terminal_key is None:
            terminal_key = project / config.TERMINAL_SECRET
        key = None
        if terminal is not None:
            key, workspaces = terminal
            if configuration is not None and not terminal_key.is_relative_to(project):
                raise TransferError("The backup's terminal key path must be inside the destination project.")
        elif ((full_checkout or {"project", "volumes"} <= restoring) and in_place_bundle is not None
                and terminal_key.is_relative_to(project) and not terminal_key.exists()):
            key = new_terminal_key()
        if key is not None:
            if in_place_bundle is not None:
                key_path = terminal_key.relative_to(project)
                if (not key_path.parts or key_path.parts[0] in CHECKOUT_RESERVED_PATHS | {".git"}
                        or key_path.parts[0].startswith(".loaf-")):
                    raise TransferError("The backup uses a reserved project path for the terminal key.")
            if full_checkout:
                staged_key = checkout / terminal_key.relative_to(project)
                if staged_key.resolve() != staged_key or staged_key.exists() or staged_key.is_symlink():
                    raise TransferError("The backup uses the terminal key path for a checkout file.")
                if not matching_terminal_key(terminal_key, key):
                    # Keep a new credential inside the staged tree until the restore succeeds.
                    with restore_terminal_key(staged_key, key):
                        pass
            else:
                if any(path == terminal_key for _, path in files):
                    raise TransferError("The backup uses the terminal key path for a configuration file.")
                matching_terminal_key(terminal_key, key)
                credential = restore_terminal_key(terminal_key, key)
        restored_checkout = checkout_targets(checkout, project) if full_checkout else []
        if in_place_bundle is not None:
            stage_backup_layout(in_place_bundle, project, staging)
        volumes = staging / "volumes.tar"
        if "volumes" in restoring:
            validate_volume_payload(volumes, workspaces)
            require_inactive_volumes(project, namespace, workspaces)
            if "images" not in restoring:
                image = image_names(namespace)[0]
                try:
                    subprocess.run(["docker", "image", "inspect", image], cwd=project,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
                except subprocess.CalledProcessError as exc:
                    raise TransferError(f"Restoring volumes without images requires the local helper image {image}.") from exc
        if with_docker_deps:
            install_backup_host_packages(checkout)
        if workspaces:
            create_workspace_volumes(project, namespace, workspaces, workspace_driver, json.loads(workspace_options))
        if "images" in restoring:
            docker_archive(project, ["docker", "image", "load"], images, "Load images.tar", send=True)
        with credential:
            if "volumes" in restoring:
                docker_archive(
                    project, volume_command(restore=True, namespace=namespace, workspaces=workspaces),
                    volumes, "Restore volumes.tar", send=True,
                )
            publish_project_files(files)
            if in_place_bundle is not None:
                publish_backup_layout(in_place_bundle, project, staging, restored_checkout)
        if terminal is not None:
            print(f"Terminal server key available at {terminal_key}.")
        return RestoredBackup(restoring, settings)


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


def model_volume_command(project: Path, namespace: str, *, restore: bool = False, delete: bool = False) -> list[str]:
    volume = f"{namespace}-ollama" if namespace else "ollama"
    subprocess.check_output(["docker", "volume", "inspect", volume], cwd=project)
    consumers = subprocess.check_output([
        "docker", "ps", "--filter", f"volume={volume}", "--format",
        '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}',
    ], cwd=project).decode().splitlines()
    deployment = namespace or "local-inference"
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


def export_models(
    project: Path, destination: Path, namespace: str, compression_level: int, stop_downloader: Callable[[], None],
) -> None:
    if destination.is_relative_to(project / ".git"):
        raise TransferError("Choose a backup directory outside .git.")
    stop_downloader()
    command = model_volume_command(project, namespace)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".loaf-backup-", dir=destination.parent) as folder:
        raw, pending = Path(folder) / "models.tar", Path(folder) / MODEL_ARCHIVE_NAME
        docker_archive(project, command, raw, "Save models")
        validate_model_archive(raw)
        with (raw.open("rb") as payload, gzip.open(pending, "wb", compresslevel=compression_level) as output,
              Progress("Compress models", raw.stat().st_size) as progress):
            copy_payload(payload.read, output, progress)
        pending.chmod(0o600)
        pending.replace(destination)


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


def import_models(
    project: Path, source: Path, namespace: str, stop_downloader: Callable[[], None], *, delete: bool = False,
) -> None:
    with TemporaryDirectory(prefix=".loaf-restore-", dir=project) as folder:
        raw = Path(folder) / "models.tar"
        with gzip.open(source, "rb") as archive, raw.open("wb") as output, Progress("Unpack models") as progress:
            copy_payload(archive.read, output, progress)
        validate_model_archive(raw)
        stop_downloader()
        command = model_volume_command(project, namespace, restore=True, delete=delete)
        docker_archive(project, command, raw, "Import models", send=True)
