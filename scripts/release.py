"""Create the annotated package release tag and push it to selected remotes."""

import argparse
from pathlib import Path
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("The release helper requires Python 3.11 or newer.")


ROOT = Path(__file__).resolve().parents[1]
METADATA = "python-package/pyproject.toml"


def git(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, check=check,
        stdout=subprocess.PIPE if capture else None,
    )


def package_version(source: str) -> str:
    project = tomllib.loads(source).get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError(f"Set the package version in {METADATA}.")
    return version


def release(args: argparse.Namespace) -> None:
    commit = git("rev-parse", "--verify", "HEAD", capture=True).stdout.strip()
    version = package_version(git("show", f"{commit}:{METADATA}", capture=True).stdout)
    staged_version = package_version(git("show", f":{METADATA}", capture=True).stdout)
    working_version = package_version((ROOT / METADATA).read_text(encoding="utf-8"))
    if staged_version != version or working_version != version:
        raise ValueError(f"Commit the version change in {METADATA} before tagging.")
    tag = f"v{version}"
    ref = f"refs/tags/{tag}"
    if git("check-ref-format", ref, check=False).returncode:
        raise ValueError(f"The package version cannot be used as a Git tag: {tag!r}.")

    found = git("show-ref", "--verify", "--quiet", ref, check=False)
    if found.returncode not in (0, 1):
        found.check_returncode()
    exists = found.returncode == 0
    if exists:
        kind = git("cat-file", "-t", ref, capture=True).stdout.strip()
        if kind != "tag":
            raise ValueError(f"Tag {tag} already exists as a lightweight tag. Use a new release version.")
        target = git("rev-parse", "--verify", f"{ref}^{{}}", capture=True).stdout.strip()
        if target != commit:
            raise ValueError(f"Tag {tag} already points to {target[:12]}. Use a new release version for {commit[:12]}.")

    remotes = tuple(dict.fromkeys(args.remotes))
    for remote in remotes:
        git("remote", "get-url", "--push", "--", remote, capture=True)

    if args.dry_run:
        action = "reuse" if exists else "create"
        print(f"Would {action} annotated tag {tag} at {commit[:12]}.")
    elif exists:
        print(f"Using existing annotated tag {tag} at {commit[:12]} with its original notes.", flush=True)
    else:
        notes = []
        if args.message is not None:
            notes = ["--message", args.message]
        elif args.file is not None:
            path = args.file if args.file == "-" else str(Path(args.file).absolute())
            notes = ["--file", path]
        if notes:
            # Preserve Markdown headings in notes supplied outside the Git editor.
            notes.insert(0, "--cleanup=whitespace")
        git("tag", "--annotate", *notes, "--", tag, commit)
        print(f"Created annotated tag {tag} at {commit[:12]}.", flush=True)

    for remote in remotes:
        if args.dry_run:
            print(f"Would push {tag} to {remote}.")
        else:
            git("push", "--no-follow-tags", "--", remote, f"{ref}:{ref}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("remotes", nargs="*", metavar="REMOTE", help="Git remotes to push the tag to")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show the tag and destinations")
    notes = parser.add_mutually_exclusive_group()
    notes.add_argument("-m", "--message", help="release notes for a new tag")
    notes.add_argument("-F", "--file", help="read release notes for a new tag from a file or '-' for stdin")
    args = parser.parse_intermixed_args()
    try:
        release(args)
    except subprocess.CalledProcessError as error:
        return max(error.returncode, 1)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
