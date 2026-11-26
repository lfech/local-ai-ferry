"""Locate deployment data, describe the installation and link the Docker project."""

import importlib.metadata
import os
from pathlib import Path
import re
import sys


DATA_DIR = Path(__file__).resolve().parent / "data"
CHECKOUT_DATA = Path("python-package/src/loaf/data")
DOCKER_PROJECT = "docker-project"
# Only the Debian package build writes this marker into its deployment data.
DEBIAN_PACKAGE_MARKER = DATA_DIR / "debian-package"
SYSTEM_PROJECT_DIR = Path("/var/lib/loaf")
DISTRIBUTION = "local-ai-ferry"


def debian_package() -> bool:
    return DEBIAN_PACKAGE_MARKER.is_file()


def source_checkout() -> Path | None:
    """Return the checkout when this launcher runs from its source tree."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / CHECKOUT_DATA).resolve() == DATA_DIR else None


def standalone_executable() -> Path | None:
    executable = os.environ.get("LOAF_RELEASE_EXECUTABLE")
    runtime = Path(sys.base_prefix).parent
    return Path(executable) if executable and (runtime / ".loaf-release").is_file() else None


def release_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        checkout = source_checkout()
        metadata = checkout / "python-package/pyproject.toml" if checkout else None
        if metadata and (match := re.search(r'(?m)^version = "([^"]+)"$', metadata.read_text(encoding="utf-8"))):
            return match[1]
        return "unknown"


def installation() -> str:
    if debian_package():
        return "Debian package"
    if (executable := standalone_executable()) is not None:
        return f"standalone executable {executable}"
    if (checkout := source_checkout()) is not None:
        return f"source checkout {checkout}"
    return "Python package"


def loaf_data_link(link: Path) -> bool:
    """Whether a link targets the deployment data of any loaf installation, possibly a removed one."""
    if not link.is_symlink():
        return False
    target = Path(os.path.realpath(link))
    return target.parts[-2:] == ("loaf", "data") and (not target.exists() or (target.parent / "cli.py").is_file())


def compose_path(project: Path) -> Path:
    for directory in (project, project / CHECKOUT_DATA):
        candidate = directory / "compose.yml"
        if candidate.is_file():
            return candidate
    link = project / DOCKER_PROJECT
    if loaf_data_link(link):
        # Use this launcher's own files, also when the link is from another release.
        return DATA_DIR / "compose.yml"
    return link / "compose.yml" if (link / "compose.yml").is_file() else project / "compose.yml"


def link_docker_project(directory: Path, *, dry_run: bool) -> None:
    if not (DATA_DIR / "compose.yml").is_file():
        raise FileNotFoundError(f"The installed Docker project is missing from {DATA_DIR}.")
    link = directory / DOCKER_PROJECT
    target = DATA_DIR.relative_to(directory) if DATA_DIR.is_relative_to(directory) else DATA_DIR
    if link.is_symlink() and link.is_dir() and link.samefile(DATA_DIR):
        print(f"The Docker project is already linked at {link}.")
        return
    stale = loaf_data_link(link)
    if not stale and (link.exists() or link.is_symlink()):
        raise FileExistsError(f"{link} already exists. Choose another directory or move the existing path.")
    action = "Update" if stale else "Link"
    if dry_run:
        print(f"{action} {link} to {target}.")
        return
    # Replace a link from another loaf release in one step.
    pending = directory / f".loaf-link-{os.getpid()}"
    pending.symlink_to(target, target_is_directory=True)
    try:
        os.replace(pending, link)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    print(f"{'Updated' if stale else 'Linked'} {link} to {target}.")
