"""Show and export the guides bundled with loaf, and render its manual page."""

import argparse
import importlib.metadata
import os
from pathlib import Path, PurePosixPath
import posixpath
import pydoc
import re
import shutil
import sys

from . import assets


# In a checkout these are links to the repository's README.md and docs/.
README = assets.DATA_DIR / "README.md"
DOCS = assets.DATA_DIR / "docs"
MANUAL = "loaf.1"
STAMP = ".loaf-docs"
EXPORT_DIR = PurePosixPath("docs")
LINK = re.compile(r"(\]\()([^)\s]+)(\))")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def sources() -> dict[PurePosixPath, Path]:
    """Map repository paths to bundled files: the README, user guides, architecture and examples."""
    files = {PurePosixPath("README.md"): README}
    for path in sorted(DOCS.glob("*.md")) + [DOCS / "instructions/architecture.md"] + sorted(DOCS.glob("examples/*")):
        if path.is_file() and not path.name.endswith("-record.md"):
            files[PurePosixPath("docs") / path.relative_to(DOCS).as_posix()] = path
    return files


def guides() -> dict[str, PurePosixPath]:
    return {
        ("readme" if path.name == "README.md" else path.stem): path
        for path in sources() if path.suffix == ".md"
    }


def export_path(path: PurePosixPath) -> PurePosixPath:
    return EXPORT_DIR / path.name if path == PurePosixPath("README.md") else path


def release_url() -> str | None:
    try:
        metadata = importlib.metadata.metadata(assets.DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None
    for entry in metadata.get_all("Project-URL") or ():
        label, _, url = entry.partition(",")
        if label.strip() == "Repository":
            return f"{url.strip()}/blob/v{assets.release_version()}"
    return None


def render(path: PurePosixPath, files: dict[PurePosixPath, Path], base_url: str | None) -> str:
    """Rewrite repository links for the exported docs/; other repository files link to the release."""
    target = export_path(path)

    def replace(match: re.Match) -> str:
        link, _, anchor = match[2].partition("#")
        if not link or SCHEME.match(link):
            return match[0]
        # A link starting with / is relative to the repository root, as on GitHub.
        base = "" if link.startswith("/") else path.parent.as_posix()
        resolved = PurePosixPath(posixpath.normpath(posixpath.join(base, link.lstrip("/"))))
        if resolved in files:
            link = posixpath.relpath(export_path(resolved), target.parent)
        elif base_url is not None:
            link = f"{base_url}/{resolved}"
        return f"{match[1]}{link}{'#' + anchor if anchor else ''}{match[3]}"

    return LINK.sub(replace, files[path].read_text(encoding="utf-8"))


def show(name: str | None) -> None:
    names = guides()
    if name is None:
        width = max(map(len, (*names, MANUAL)))
        for guide, path in names.items():
            title = next((line[2:] for line in sources()[path].read_text(encoding="utf-8").splitlines()
                          if line.startswith("# ")), guide)
            print(f"  {guide:<{width}}  {title}")
        print(f"  {MANUAL:<{width}}  Manual page")
        print("\nTo read a guide:       loaf docs <page>")
        print(  "View a markdown file:  loaf docs <page> | glow -p -")
        print(  "View the manual page:  loaf docs loaf.1 | man -l -")
        return
    if name == MANUAL:
        print(manual_page(), end="")
        return
    if name not in names:
        raise ValueError(f"No guide named {name!r}. Run 'loaf docs' for the list.")
    text = render(names[name], sources(), release_url())
    if sys.stdout.isatty():
        pydoc.pager(text)
    else:
        print(text, end="")


def export(project: Path, *, force: bool = False) -> None:
    """Write the guides for this release to docs/. An existing docs/ stays unless force replaces it.

    Guides that this release exported before stay as they are.
    """
    target = project / EXPORT_DIR
    version = assets.release_version()
    stamp = target / STAMP
    if target.exists() or target.is_symlink():
        current = not target.is_symlink() and stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == version
        if current and not force:
            return
        if not force:
            print(f"Keep the existing {target}. Add --force to replace it with the guides for loaf {version}, "
                  "or run 'loaf docs' to read them.")
            return
        if target.is_symlink() or not target.is_dir():
            target.unlink()
    files = sources()
    base_url = release_url()
    staging = project / f".loaf-docs-{os.getpid()}"
    try:
        for path in files:
            destination = staging / export_path(path).relative_to(EXPORT_DIR)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".md":
                destination.write_text(render(path, files, base_url), encoding="utf-8")
            else:
                shutil.copyfile(files[path], destination)
        (staging / MANUAL).write_text(manual_page(), encoding="utf-8")
        (staging / STAMP).write_text(version + "\n", encoding="utf-8")
        if target.exists():
            previous = project / f".loaf-docs-{os.getpid()}-previous"
            target.rename(previous)
            staging.rename(target)
            shutil.rmtree(previous)
        else:
            staging.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f"Guides for loaf {version} are in {target}.")


def roff(text: str) -> str:
    text = text.replace("\\", "\\e").replace("-", "\\-")
    return "\n".join("\\&" + line if line.startswith((".", "'")) else line for line in text.splitlines())


def manual_page() -> str:
    """Render the manual page: each command with the names of its options, then every option once."""
    from . import cli

    parser = argparse.ArgumentParser(add_help=False)
    cli.add_options(parser)
    actions = [action for action in parser._actions if action.help != argparse.SUPPRESS]
    options = {flag: action for action in actions for flag in action.option_strings}

    def option_items(selected) -> list[str]:
        items = []
        for action in selected:
            metavar = f" {action.metavar}" if action.metavar else ""
            items += [".TP", f"\\fB{roff(', '.join(action.option_strings))}\\fR{roff(metavar)}", roff(action.help or "")]
        return items

    def command_items(command, subcommand, details) -> list[str]:
        name = " ".join(part for part in (command, subcommand) if part)
        items = [".TP", f"\\fB{roff(name)}\\fR {roff(cli.command_arguments(command, subcommand))}", roff(details.summary)]
        if details.options:
            names = ", ".join(f"\\fB{roff(flag)}\\fR" for flag in details.options)
            items += [".br", f"Options: {names}."]
        return items

    lines = [
        f'.TH LOAF 1 "" "loaf {assets.release_version()}" "Local AI Ferry"',
        ".SH NAME", "loaf \\- prepare, serve and transfer local Ollama and Open WebUI deployments",
        ".SH SYNOPSIS", "\\fBloaf\\fR [\\fIGLOBAL OPTIONS\\fR] \\fICOMMAND\\fR [\\fIOPTIONS\\fR]",
        ".SH DESCRIPTION", roff(
            "loaf runs Ollama and Open WebUI with Docker Compose on Debian, prepares models while connected "
            "and moves complete deployments to offline hosts. A deployment reads the packaged defaults, then "
            "the system settings in /etc/loaf, then the project's config/config.ini. Later layers override "
            "earlier ones. The Debian package operates the system deployment unless --project-dir selects "
            "a project."
        ),
        ".SH COMMANDS",
    ]
    for name, details in cli.COMMAND_HELP.items():
        lines += command_items(name, None, details)
        for subcommand, sub in cli.SUBCOMMAND_HELP.get(name, {}).items():
            lines += [".RS", *command_items(name, subcommand, sub), ".RE"]
    global_options = [options[flag] for flag in cli.GLOBAL_OPTIONS]
    lines += [".SH OPTIONS", roff("The commands above name the options they accept."),
              *option_items(action for action in actions if action.option_strings and action not in global_options)]
    lines += [".SH GLOBAL OPTIONS", *option_items(global_options)]
    lines += [
        ".SH FILES",
        ".TP", "\\fI/etc/loaf/config.ini\\fR, \\fI/etc/loaf/models.ini\\fR", "System settings and model list.",
        ".TP", "\\fI/var/lib/loaf\\fR", "System deployment data, backups and lock.",
        ".TP", "\\fIconfig/config.ini\\fR, \\fIconfig/models.ini\\fR", "A project's settings and model list.",
        ".TP", "\\fI~/.local/share/loaf/runtime\\fR", "Runtime unpacked by the standalone executable.",
        ".SH SEE ALSO",
        roff("Run 'loaf docs' for the bundled guides, including installation, configuration and transfer."),
    ]
    return "\n".join(lines) + "\n"
