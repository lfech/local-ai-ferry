"""Remove regenerable loaf state from the project and the user runtime cache."""

from contextlib import ExitStack
import os
from typing import Callable
from pathlib import Path
import shutil
import subprocess
import tarfile
import zlib

from . import assets, config, locks, setup, transfer


STAGING_PREFIXES = (".loaf-backup-", ".loaf-restore-", ".loaf-toolkit-", ".loaf-badges-", ".loaf-bootstrap-",
                    ".loaf-terminal-key-", ".loaf-wheels-", ".loaf-docker-", ".loaf-binary-", ".loaf-link-", ".loaf-docs-",
                    ".loaf-repository-", ".loaf-download-")
FULL_PATHS = (".venv", "dist", "install-cache", "python-package/build", "python-package/dist")
# Full paths that an offline host cannot download again; clean asks before removing them.
OFFLINE_ONLY_PATHS = ("install-cache",)
PROTECTED_NAMES = {".git", "backup", transfer.CHECKSUMS, ".env", config.TERMINAL_SECRET.name}
# Staged archive members inside backup and restore directories.
STAGED_MEMBERS = {Path(name).name for name in transfer.MEMBER_NAMES}
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar")


def local_directory(path: Path) -> bool:
    return path.is_dir() and path.resolve() == path


# The docker-project link of releases before 0.6.0 to a loaf's deployment files; clean --full removes it.
LEGACY_LINK = "docker-project"


def managed_link(project: Path, runtime: Path) -> Path | None:
    link = project / LEGACY_LINK
    if link.is_symlink():
        target = link.resolve()
        if target == assets.DATA_DIR or (
            target.is_relative_to(runtime / "runtime") and target.parts[-2:] == ("loaf", "data")
        ):
            return link
    return None


def project_targets(project: Path, runtime: Path, full: bool) -> set[Path]:
    link = managed_link(project, runtime)
    package = project / setup.PYTHON_PACKAGE_DIR
    source = local_directory(package) and (package / "src/loaf/__init__.py").is_file()
    # Standalone setup leaves settings; a project from before 0.6.0 may only have the docker-project link.
    settings = (project / "config" / config.CONFIG_NAME).is_file()
    if not (source or link or settings or transfer.is_bundle(project)):
        return set()

    targets = {project / ".test-artifacts", project / "__pycache__"}
    for directory in (
        project, project / "backup", project / "config", project / "dist",
        project / setup.PYTHON_WHEEL_DIR, project / setup.DOCKER_DEB_DIR, project / setup.PYTHON_RUNTIME_DIR,
    ):
        if local_directory(directory):
            targets.update(path for path in directory.iterdir() if path.name.startswith(STAGING_PREFIXES))

    for directory in (project / "python-package", project / "scripts", project / ".venv"):
        if not local_directory(directory):
            continue
        for root, folders, files in os.walk(directory):
            parent = Path(root)
            if PROTECTED_NAMES.intersection(folders + files) or transfer.is_bundle(parent):
                folders.clear()
                continue
            if "__pycache__" in folders:
                folders.remove("__pycache__")
                targets.add(parent / "__pycache__")
            targets.update(parent / name for name in files if name.endswith((".pyc", ".pyo")))

    if local_directory(project / "config"):
        for path in (project / "config").rglob("*.model.tmp.*"):
            if path.is_file() and local_directory(path.parent):
                targets.add(path)
    if full:
        targets.update(project / name for name in FULL_PATHS)
        if local_directory(package):
            targets.update(package.glob("*.egg-info"))
            if local_directory(package / "src"):
                targets.update((package / "src").glob("*.egg-info"))
        if link is not None:
            targets.add(link)
        launcher = project / "loaf"
        if launcher.is_symlink() and launcher.readlink() == setup.CHECKOUT_LAUNCHER:
            targets.add(launcher)
    return {path for path in targets if local_directory(path.parent) and (path.exists() or path.is_symlink())}


def checkout_files(project: Path) -> set[Path]:
    for folder in (project, *project.parents):
        marker = folder / ".git"
        if marker.is_file() or (marker / "HEAD").is_file() or (folder == project and marker.exists()):
            break
        if folder.is_mount():
            return set()
    else:
        return set()
    git = ["git", "-c", "core.fsmonitor=false"]
    entries = subprocess.check_output([
        *git, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
    ], cwd=project)
    # git rm --cached can leave a working copy under a Git-ignored directory.
    entries += subprocess.check_output([
        *git, "diff", "--cached", "--relative", "--no-ext-diff", "--name-only", "--diff-filter=D", "-z",
    ], cwd=project)
    return {project / os.fsdecode(entry) for entry in entries.split(b"\0") if entry}


def protected_files(project: Path) -> set[Path]:
    paths = checkout_files(project)
    # An unreadable configuration can hide a key inside a cleanup target.
    settings = config.load(*config.layers(assets.DATA_DIR / "config", project / "config"))
    paths.add(config.terminal_secret_path(project, settings.terminal_server_secret_file))
    return paths


def first_gzipped_tar_member(path: Path) -> tarfile.TarInfo | None:
    if path.is_symlink():
        return None
    try:
        with tarfile.open(path, "r|gz") as archive:
            return archive.next()
    except (OSError, EOFError, tarfile.TarError, zlib.error):
        return None


def release_source_archive(path: Path) -> bool:
    if not (path.name.startswith("loaf-v") and path.name.endswith("-source.tar.gz")):
        return False
    first = first_gzipped_tar_member(path)
    return first is not None and first.isdir() and first.name == path.name.removesuffix("-source.tar.gz")


def possible_archive(path: Path) -> bool:
    if path.name.endswith(ARCHIVE_SUFFIXES):
        return True
    if path.is_symlink() or not path.is_file():
        return False
    # An explicit --archive path can have any extension. Both backup types use gzip.
    with path.open("rb") as payload:
        gzip_magic = payload.read(2) == b"\x1f\x8b"
    return gzip_magic and first_gzipped_tar_member(path) is not None


def contains_saved_data(path: Path, runtime: Path, downloads: Path | None = None) -> bool:
    """Whether a cleanup target holds Git work, backups, credentials or a mount.

    downloads is the directory of checked runtime downloads, whose archives are not backups.
    """
    if path.name in PROTECTED_NAMES or possible_archive(path):
        return True
    if path.is_symlink() or not path.is_dir():
        return False

    def raise_walk_error(error: OSError) -> None:
        raise error

    for root, directories, filenames in os.walk(path, onerror=raise_walk_error):
        current = Path(root)
        if current.is_mount() or PROTECTED_NAMES.intersection(directories + filenames) or transfer.is_bundle(current):
            return True
        for name in filenames:
            if name in STAGED_MEMBERS and any(
                part.startswith((".loaf-backup-", ".loaf-restore-")) for part in current.parts
            ):
                continue
            # These are the pinned runtime inputs inside a private build stage, and their cached downloads.
            if current.parent == runtime and current.name.startswith(".build-") and name in (
                "python.tar.gz", "licenses.tar.gz", "payload.tar.gz",
            ):
                continue
            if current == downloads and name.endswith(".tar.gz"):
                continue
            if possible_archive(current / name) and not release_source_archive(current / name):
                return True
    return False


def clean(project: Path, *, full: bool, dry_run: bool, confirm: Callable[[Path], bool] = lambda path: False) -> None:
    """Remove regenerable state. confirm decides about paths an offline host cannot download again."""
    home = locks.data_home().resolve()
    runtime = home / "loaf"
    if project.is_relative_to(runtime) or runtime.is_symlink():
        raise OSError(f"Choose a local loaf runtime cache outside the selected project: {runtime}")
    if runtime.exists() and (
        not runtime.is_dir() or (hasattr(os, "geteuid") and runtime.stat().st_uid != os.geteuid())
    ):
        raise OSError(f"The loaf runtime cache must be a directory owned by this user: {runtime}")
    if not project.is_dir():
        raise FileNotFoundError(f"Project directory does not exist: {project}")

    home_exists = home.is_dir()
    with ExitStack() as stack:
        if not dry_run and home_exists:
            stack.enter_context(locks.runtime_cleanup_lock(home))
        targets = project_targets(project, runtime, full)
        if targets and not dry_run:
            stack.enter_context(locks.project_lock(project / locks.PROJECT_LOCK))
            targets = project_targets(project, runtime, full)
        if home_exists and runtime.exists():
            targets.add(runtime)
        protected = protected_files(project) if targets else set()
        planned = []
        for path in sorted(targets, key=lambda path: (path == runtime, len(path.parts), path)):
            if not (path.exists() or path.is_symlink()) or any(path.is_relative_to(item) for item in planned):
                continue
            if any(item.is_relative_to(path) for item in protected) or contains_saved_data(
                    path, runtime, project / setup.PYTHON_RUNTIME_DIR):
                print(f"Keep {path} (protected Git work, backup, credentials or mount).")
                continue
            if path in {project / name for name in OFFLINE_ONLY_PATHS}:
                if dry_run:
                    print(f"Would ask before removing {path}, whose installers an offline host cannot download again")
                    continue
                if not confirm(path):
                    continue
            planned.append(path)

        for path in planned:
            if dry_run:
                print(f"Would remove {path}")
            elif path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)
        if not planned:
            print("Nothing to clean.")
        elif not dry_run:
            print(f"Removed {len(planned)} generated paths.")
