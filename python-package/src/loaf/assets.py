"""Locate deployment data and expose it for direct Docker Compose use."""

from pathlib import Path
from tempfile import TemporaryDirectory

from .locks import data_home


DATA_DIR = Path(__file__).resolve().parent / "data"
CHECKOUT_DATA = Path("python-package/src/loaf/data")
DOCKER_PROJECT = "docker-project"


def compose_path(project: Path) -> Path:
    local = project / "compose.yml"
    for directory in (project, project / CHECKOUT_DATA, project / DOCKER_PROJECT):
        candidate = directory / local.name
        if candidate.is_file():
            return candidate
    return local


def legacy_docker_link(directory: Path) -> bool:
    link = directory / DOCKER_PROJECT
    if not link.is_symlink():
        return False
    target = link.resolve()
    old_runtime = data_home().resolve() / "owo/runtime"
    return target == directory / "python-package/src/owo/data" or (
        target.is_relative_to(old_runtime) and target.parts[-2:] == ("owo", "data")
    )


def link_docker_project(directory: Path, *, dry_run: bool) -> None:
    if not (DATA_DIR / "compose.yml").is_file():
        raise FileNotFoundError(f"The installed Docker project is missing from {DATA_DIR}.")
    link = directory / DOCKER_PROJECT
    target = DATA_DIR.relative_to(directory) if DATA_DIR.is_relative_to(directory) else DATA_DIR
    if dry_run:
        print(f"Link {link} to {target}.")
        return
    if legacy_docker_link(directory):
        with TemporaryDirectory(prefix=".loaf-link-", dir=directory) as staging:
            pending = Path(staging) / DOCKER_PROJECT
            pending.symlink_to(target, target_is_directory=True)
            if not legacy_docker_link(directory):
                raise FileExistsError(f"The Docker project link changed at {link}. Retry the command.")
            pending.replace(link)
        print(f"Updated {link} to {target}.")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except FileExistsError:
        if not (link.is_symlink() and link.is_dir() and link.samefile(DATA_DIR)):
            raise FileExistsError(f"{link} already exists. Choose another directory or move the existing path.") from None
        print(f"The Docker project is already linked at {link}.")
    else:
        print(f"Linked {link} to {target}.")
