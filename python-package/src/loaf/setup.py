"""Prepare the launcher and its Debian host installer cache."""

from contextlib import suppress
from hashlib import sha256
import json
import os
from pathlib import Path
from platform import freedesktop_os_release
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkdtemp
from typing import Iterable, Iterator
from urllib.request import urlopen

from . import assets, config


PYTHON_PACKAGE_DIR = Path("python-package")
INSTALL_CACHE = Path("install-cache")
PYTHON_WHEEL_DIR = INSTALL_CACHE / "wheels"
TOOLKIT_DIR = INSTALL_CACHE / "deb/nvidia"
DOCKER_DEB_DIR = INSTALL_CACHE / "deb/docker"
DOCKER_PACKAGES = ("docker-cli", "docker-compose", "docker.io", "docker-buildx")
WHEEL_CACHE_RECORD = ".loaf-wheel-cache.json"
TOOLKIT_PACKAGES = (
    "nvidia-container-toolkit",
    "nvidia-container-toolkit-base",
    "libnvidia-container-tools",
    "libnvidia-container1",
)
APT_CACHE = Path("/var/cache/apt/archives")
NVIDIA_REPOSITORY = "https://nvidia.github.io/libnvidia-container"
READ_WHEEL_INPUTS = """\
import json
from pathlib import Path
import platform
import sys
import sysconfig

try:
    import tomllib as toml
except ModuleNotFoundError:
    # Python 3.10 can use the TOML parser bundled with its pip.
    try:
        from pip._vendor import tomli as toml
    except ImportError:
        from pip._vendor import toml

metadata = toml.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(json.dumps([
    metadata["build-system"]["requires"], sys.version.split()[0],
    sys.implementation.name, sysconfig.get_platform(), sysconfig.get_config_var("SOABI"),
    platform.libc_ver(),
]))
"""
CHECK_VENV = """\
import os
import sys

home = getattr(sys, "_home", None)
if home and (not os.path.isdir(home) or not os.access(home, os.X_OK)):
    sys.exit(1)
print(sys.prefix)
"""


class SetupError(ValueError):
    """The host or project prerequisites could not be prepared."""


def bundled_release() -> Path | None:
    directory = Path(sys.base_prefix).parent
    return directory if (directory / ".loaf-release").is_file() else None


def setup_standalone(project: Path) -> None:
    directory = project / "config"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise SetupError(f"{directory} must be a local configuration directory.")
    if not assets.compose_path(project).is_file():
        assets.link_docker_project(project, dry_run=False)
    directory.mkdir(exist_ok=True)
    for name in ("config.ini", "models.ini"):
        source = assets.DATA_DIR / "config" / name
        target = directory / name
        try:
            defaults = source.read_bytes()
            with target.open("xb") as output:
                output.write(defaults)
        except FileExistsError:
            if not target.is_file():
                raise SetupError(f"{target} must be a configuration file.") from None


def ensure_terminal_key(path: Path, archived_key: bytes | None = None) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, prefix=".loaf-terminal-key-") as pending:
            pending.write(archived_key if archived_key is not None else new_terminal_key())
            pending.flush()
            try:
                os.link(pending.name, path)
            except FileExistsError:
                pass
            else:
                print(f"Created terminal server key at {path}.")
    config.read_terminal_secret(path)


def new_terminal_key() -> bytes:
    return secrets.token_hex(32).encode("ascii")


def install_backup_host_packages(project: Path) -> None:
    prepare_docker(project, allow_downloads=False)
    if any((project / TOOLKIT_DIR).glob("*.deb")):
        install_toolkit(project, allow_downloads=False)


def installed_toolkit() -> dict[str, tuple[str, str]]:
    if not shutil.which("dpkg-query"):
        return {}
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\\n",
         *(f"{name}:{arch}" for name in TOOLKIT_PACKAGES)],
        capture_output=True, text=True,
    )
    packages = {}
    for line in result.stdout.splitlines():
        name, version, arch, status = line.split("\t")
        if status == "installed":
            packages[name] = version, arch
    return packages


def deb_packages(directories: Iterable[Path]) -> Iterator[tuple[tuple[str, str, str], Path]]:
    for directory in directories:
        for path in sorted(directory.glob("*.deb")):
            if path.name.split("_", 1)[0] not in TOOLKIT_PACKAGES:
                continue
            output = subprocess.check_output([
                "dpkg-deb", "--show", "--showformat=${Package} ${Version} ${Architecture}", str(path),
            ], text=True)
            name, version, arch = output.split()
            if name in TOOLKIT_PACKAGES:
                yield (name, version, arch), path


def staged_toolkit(directories: Iterable[Path]) -> list[Path]:
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    versions = {}
    for (name, version, package_arch), path in deb_packages(directories):
        if package_arch == arch:
            versions.setdefault(version, {}).setdefault(name, path)
    latest = None
    for version, packages in versions.items():
        if packages.keys() >= set(TOOLKIT_PACKAGES) and (
            latest is None or subprocess.run(
                ["dpkg", "--compare-versions", version, "gt", latest], check=False,
            ).returncode == 0
        ):
            latest = version
    return list(versions[latest].values()) if latest else []


def cache_directory(project: Path, relative: Path) -> Path:
    directory = project / relative
    if directory.resolve() != directory.absolute() or (directory.exists() and not directory.is_dir()):
        raise SetupError(f"{directory} must be a local directory.")
    return directory


def toolkit_files(project: Path, *, download_to: Path | None = None) -> list[Path]:
    """Locate the installed package versions, or a complete prepared toolkit."""
    if not shutil.which("dpkg-deb"):
        return []
    directories = [cache_directory(project, TOOLKIT_DIR)]
    installed = installed_toolkit()
    if not installed:
        return staged_toolkit(directories)
    if not all(name in installed for name in TOOLKIT_PACKAGES):
        raise SetupError("The installed NVIDIA Container Toolkit package set is incomplete.")
    if download_to is not None:
        directories.insert(0, download_to)
    candidates = dict(deb_packages([APT_CACHE, *reversed(directories)]))
    keys = [(name, *installed[name]) for name in TOOLKIT_PACKAGES]
    missing = [key for key in keys if key not in candidates]
    if missing and download_to is not None:
        subprocess.run(
            ["apt-get", "download", *(f"{name}:{arch}={version}" for name, version, arch in missing)],
            cwd=download_to, check=True,
        )
        candidates.update(deb_packages([download_to]))
        missing = [key for key in keys if key not in candidates]
    if missing:
        raise SetupError("Installer packages for the installed NVIDIA toolkit are missing. Run loaf setup while connected.")
    return [candidates[key] for key in keys]


def apt(*args: str) -> None:
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    options = ["--yes", "--no-remove"] if "install" in args else []
    subprocess.run([*prefix, "apt-get", *options, *args], check=True)


def add_nvidia_repository() -> None:
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    sources = Path("/etc/apt/sources.list.d")
    repository = "loaf-nvidia-container-toolkit"
    keyring = f"/usr/share/keyrings/{repository}.asc"
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    with TemporaryDirectory(prefix="loaf-apt-") as folder:
        directory = Path(folder)
        key = directory / "nvidia.asc"
        with urlopen(f"{NVIDIA_REPOSITORY}/gpgkey", timeout=30) as response, key.open("wb") as output:
            shutil.copyfileobj(response, output)
        source = directory / f"{repository}.list"
        source.write_text(f"deb [signed-by={keyring}] {NVIDIA_REPOSITORY}/stable/deb/{arch} /\n", encoding="utf-8")
        subprocess.run([*prefix, "install", "-m", "0644", str(key), keyring], check=True)
        subprocess.run([
            *prefix, "install", "-m", "0644", str(source), str(sources / source.name),
        ], check=True)


def install_toolkit(project: Path, *, allow_downloads: bool = True) -> None:
    cache = cache_directory(project, TOOLKIT_DIR)
    with TemporaryDirectory(prefix=".loaf-toolkit-", dir=project) as folder:
        staging = Path(folder)
        installed = installed_toolkit()
        if not shutil.which("nvidia-ctk") or not all(name in installed for name in TOOLKIT_PACKAGES):
            packages = staged_toolkit([cache])
            if packages:
                print(f"Install NVIDIA Toolkit from local packages in {packages[0].parent}…", file=sys.stderr, flush=True)
                # Let APT stage the local files with package repositories disabled.
                sources = staging / "sources.list"
                sources.touch()
                apt(
                    "install", "--no-install-recommends", "-o", f"Dir::Etc::sourcelist={sources}",
                    "-o", f"Dir::Etc::sourceparts={staging}", *(str(path.absolute()) for path in packages),
                )
            elif allow_downloads:
                apt("update")
                candidate = subprocess.run(
                    ["apt-cache", "show", TOOLKIT_PACKAGES[0]], capture_output=True,
                )
                if candidate.returncode or not candidate.stdout:
                    add_nvidia_repository()
                    apt("update")
                apt("install", "--no-install-recommends", *TOOLKIT_PACKAGES)
            else:
                raise SetupError(f"NVIDIA toolkit installers for this host are missing from {cache}.")
            print("Toolkit installed. Configure the Docker runtime if needed. See docs/installation.md.")
        files = toolkit_files(project, download_to=staging if allow_downloads else None)
        if not files:
            print("The existing toolkit is available. No Debian installer packages were found for backup.")
            return
        cache.mkdir(parents=True, exist_ok=True)
        for path in files:
            target = cache / path.name
            if target.is_file() and not target.is_symlink() and target.samefile(path):
                continue
            temporary = staging / path.name
            if path != temporary:
                shutil.copyfile(path, temporary)
            temporary.replace(target)
    print(f"NVIDIA toolkit installers ready in {cache}")


def docker_cache(project: Path) -> Path:
    release = freedesktop_os_release()
    suite = release.get("VERSION_CODENAME", "")
    if release.get("ID") != "debian" or suite != "trixie":
        raise SetupError("--with-docker-deps requires Debian 13 (trixie). See docs/debian.md for other Docker installations.")
    architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    if not re.fullmatch(r"[a-z0-9-]+", architecture):
        raise SetupError("Could not determine the Debian package architecture.")
    return cache_directory(project, DOCKER_DEB_DIR / f"{suite}-{architecture}")


def download_docker_packages(cache: Path) -> None:
    """Resolve the package dependencies independently of the preparation host's installed packages."""
    apt("update")
    cache.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".loaf-docker-", dir=cache.parent) as folder:
        staging = Path(folder)
        status = staging / "status"
        status.touch()
        packages = staging / "packages"
        packages.mkdir()
        subprocess.run([
            "apt-get", "--yes", "--no-remove", "--no-install-recommends", "--download-only",
            "-o", "Debug::NoLocking=true", "-o", f"Dir::State::status={status}",
            "-o", f"Dir::Cache::archives={packages}", "install", *DOCKER_PACKAGES,
        ], check=True)
        with (packages / "Packages").open("w", encoding="utf-8") as index:
            found = set()
            for path in sorted(packages.glob("*.deb")):
                control = subprocess.check_output(["dpkg-deb", "--field", str(path)], text=True).rstrip()
                found.update(re.findall(r"(?m)^Package: (\S+)$", control))
                index.write(f"{control}\nFilename: ./{path.name}\nSize: {path.stat().st_size}\n"
                            f"SHA256: {file_digest(path)}\n\n")
            if not set(DOCKER_PACKAGES) <= found:
                raise SetupError("APT did not download a complete Debian Docker package set.")
        partial = packages / "partial"
        if partial.is_dir():
            partial.rmdir()
        packages.rename(cache)


def install_docker_packages(cache: Path) -> None:
    with TemporaryDirectory(prefix="loaf-docker-apt-") as folder:
        directory = Path(folder)
        sources = directory / "sources.list"
        sources.write_text(f"deb [trusted=yes] {cache.as_uri()} ./\n", encoding="utf-8")
        lists = directory / "lists"
        lists.mkdir()
        options = (
            "-o", f"Dir::Etc::sourcelist={sources}", "-o", "Dir::Etc::sourceparts=-",
            "-o", f"Dir::State::lists={lists}", "-o", "APT::Get::List-Cleanup=false",
        )
        apt("update", *options)
        apt("install", "--no-install-recommends", *options, *DOCKER_PACKAGES)


def prepare_docker(project: Path, *, allow_downloads: bool = True) -> None:
    cache = docker_cache(project)
    if not cache.exists():
        if not allow_downloads:
            raise SetupError(f"Docker installers for this host are missing from the backup: {cache}.")
        print("Download Debian Docker packages and their dependencies…", file=sys.stderr, flush=True)
        download_docker_packages(cache)
    if not (cache / "Packages").is_file() or (cache / "Packages").is_symlink():
        raise SetupError(f"Incomplete Docker installer cache at {cache}.")
    print(f"Debian Docker installers ready in {cache}")
    if shutil.which("dockerd") and all(command_works(command) for command in (
        ["docker", "--version"], ["docker", "compose", "version"], ["docker", "buildx", "version"],
    )):
        print("Docker Engine, Compose and Buildx are already installed.")
    else:
        print("Install Debian Docker packages from the local cache…", file=sys.stderr, flush=True)
        install_docker_packages(cache)


def docker_installer_files(project: Path) -> Iterator[Path]:
    directory = cache_directory(project, DOCKER_DEB_DIR)
    for cache in sorted(directory.glob("*")):
        index = cache / "Packages"
        if not cache.is_symlink() and index.is_file() and not index.is_symlink():
            yield index
            yield from (path for path in sorted(cache.glob("*.deb")) if path.is_file() and not path.is_symlink())


def interpreter_works(python: Path, environment: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", CHECK_VENV],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == environment


def command_works(command: list[str]) -> bool:
    try:
        return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def ensure_venv_support(python: Path) -> None:
    probe = subprocess.run(
        [str(python), "-I", "-c", "import ensurepip, venv"], capture_output=True,
    )
    if probe.returncode == 0:
        return
    if shutil.which("dpkg-query") and subprocess.run(
        ["dpkg-query", "-S", str(python.resolve())], capture_output=True,
    ).returncode == 0:
        version = subprocess.check_output([
            str(python), "-I", "-c", 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")',
        ], text=True).strip()
        apt("install", f"python{version}-venv")
    else:
        raise SetupError(f"{python} lacks ensurepip or venv support. Select a Python build that includes both.")


def environment_relocated(environment: Path) -> bool:
    config = environment / "pyvenv.cfg"
    if not config.is_file():
        return False
    for line in config.read_text(encoding="utf-8").splitlines():
        if line.startswith("command = "):
            return not line.endswith(f" {environment}")
    # Python 3.10 may record the creation path only in the activation script.
    activate = environment / "bin/activate"
    if activate.is_file():
        for line in activate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("VIRTUAL_ENV="):
                try:
                    fields = shlex.split(line.partition("=")[2])
                except ValueError:
                    return True
                if len(fields) == 1 and Path(fields[0]).is_absolute():
                    return Path(fields[0]) != environment
    return False


def pip_command(python: Path) -> list[str]:
    return [str(python), "-I", "-m", "pip", "--disable-pip-version-check", "--no-input"]


def venv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def local_pip_environment() -> dict[str, str]:
    # Keep configured indexes and extra links out of local-only pip calls.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def run_python_step(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
    )


def check_python_step(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode and result.stdout:
        print(result.stdout.rstrip(), file=sys.stderr, flush=True)
    result.check_returncode()


def file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_digest(source: Path) -> str:
    digest = sha256()
    for path in sorted(source.rglob("*")):
        if path.is_file():
            digest.update(os.fsencode(path.relative_to(source)) + b"\0")
            digest.update(bytes.fromhex(file_digest(path)))
    return digest.hexdigest()


def wheels_are_current(wheels: Path, inputs: dict) -> bool:
    manifest = wheels / WHEEL_CACHE_RECORD
    if manifest.is_symlink():
        return False
    try:
        record = json.loads(manifest.read_bytes())
    except (FileNotFoundError, ValueError):
        return False
    if not isinstance(record, dict) or record.get("inputs") != inputs:
        return False
    expected = record.get("wheels")
    existing = {path.name: path for path in wheels.glob("*.whl") if path.is_file() and not path.is_symlink()}
    return isinstance(expected, dict) and bool(expected) and expected.keys() <= existing.keys() and all(
        file_digest(existing[name]) == digest for name, digest in expected.items()
    )


def prepare_python_wheels(project: Path, python: Path | None = None, *, allow_downloads: bool = False) -> Path:
    """Export the current launcher and its declared requirements as wheels."""
    if python is None:
        environment = project / ".venv"
        python = venv_python(environment)
        current_python = Path(sys.executable)
        if not interpreter_works(python, environment):
            if current_python.is_relative_to(environment):
                raise SetupError("The project virtualenv needs repair. Run loaf install with the host's Python.")
            python = current_python
        elif not command_works([*pip_command(python), "--version"]):
            python = current_python
    wheels = cache_directory(project, PYTHON_WHEEL_DIR)
    package = project / PYTHON_PACKAGE_DIR
    generated = shutil.ignore_patterns("wheels", "deb", "build", "dist", "*.egg-info", "__pycache__")

    def ignore_build_files(directory: str, names: list[str]) -> set[str]:
        ignored = generated(directory, names)
        if Path(directory) == package:
            ignored.add("tests")
        return ignored

    wheels.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".loaf-wheels-", dir=wheels) as folder:
        staging = Path(folder)
        # Build a fresh source copy so an old setuptools build cannot leak into the wheel.
        source = staging / "source"
        shutil.copytree(package, source, ignore=ignore_build_files)
        build_requirements, *interpreter = json.loads(subprocess.check_output([
            str(python), "-I", "-c", READ_WHEEL_INPUTS, str(source / "pyproject.toml"),
        ]))
        inputs = {"project": package_digest(source), "python": interpreter}
        if wheels_are_current(wheels, inputs):
            print("Reuse the cached Python wheels.", file=sys.stderr, flush=True)
            return wheels

        print("Build the launcher wheel and cache Python requirements…", file=sys.stderr, flush=True)
        command = [
            *pip_command(python), "wheel", "--wheel-dir", str(staging),
            "--find-links", str(wheels), "--find-links", str(staging),
        ]
        if (release := bundled_release()) is not None:
            command.extend(("--find-links", str(release / "wheels")))
        # Build and runtime requirements have separate environments and can use different versions.
        for requirements in (build_requirements, [str(source)]):
            if not requirements:
                continue
            result = run_python_step(
                [*command, "--no-index", "--", *requirements], env=local_pip_environment(),
            )
            if result.returncode == 0:
                continue
            if not allow_downloads:
                raise SetupError(
                    "Cannot export Python wheels from the local cache. "
                    "Prepare them while connected as described in docs/launcher.md.\n"
                    + result.stdout.rstrip()
                )
            print("Fetch missing Python requirements…", file=sys.stderr, flush=True)
            check_python_step(run_python_step([*command, "--", *requirements]))
        built = sorted(staging.glob("*.whl"))
        record = staging / WHEEL_CACHE_RECORD
        record.write_text(json.dumps({
            "inputs": inputs, "wheels": {wheel.name: file_digest(wheel) for wheel in built},
        }), encoding="utf-8")
        for wheel in built:
            wheel.replace(wheels / wheel.name)
        record.replace(wheels / record.name)
    return wheels


def install_python(project: Path) -> None:
    environment = project / ".venv"
    if environment.is_symlink() or (environment.exists() and not (environment / "pyvenv.cfg").is_file()):
        raise SetupError(f"{environment} is not a local Python virtualenv.")
    python = venv_python(environment)
    rebuild = environment_relocated(environment) or not interpreter_works(python, environment)
    previous = None
    if rebuild:
        base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        ensure_venv_support(base)
        if environment.exists():
            previous = Path(mkdtemp(prefix=".venv-before-install-", dir=project)) / ".venv"
            environment.rename(previous)
    try:
        if rebuild:
            print("Create the project virtualenv…", file=sys.stderr, flush=True)
            check_python_step(run_python_step([str(base), "-m", "venv", str(environment)]))
        entry = environment / "bin/loaf"
        pip = pip_command(python)
        if not command_works([*pip, "--version"]):
            ensure_venv_support(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
            print("Install pip in the project virtualenv…", file=sys.stderr, flush=True)
            check_python_step(run_python_step([str(python), "-I", "-m", "ensurepip"]))
        wheels = prepare_python_wheels(project, python, allow_downloads=True)
        marker = environment / ".loaf-editable"
        package = project / PYTHON_PACKAGE_DIR
        metadata = file_digest(package / "pyproject.toml").encode("ascii")
        # Editable installs track source changes. Package metadata still needs reinstalling when it changes.
        if (
            marker.is_file() and not marker.is_symlink() and marker.read_bytes() == metadata
            and os.access(entry, os.X_OK)
            and command_works([*pip, "check"])
            and command_works([
                str(python), "-I", "-c",
                "from pathlib import Path; import loaf, sys; sys.exit(Path(loaf.__file__).resolve() != Path(sys.argv[1]))",
                str((package / "src/loaf/__init__.py").resolve()),
            ])
        ):
            print("The editable launcher is already installed.", file=sys.stderr, flush=True)
        else:
            print("Install the editable launcher from the checkout and cached wheels…", file=sys.stderr, flush=True)
            marker.unlink(missing_ok=True)
            check_python_step(run_python_step([
                *pip, "install", "--no-index", "--find-links", str(wheels), "-e", str(package),
            ], env=local_pip_environment()))
            marker.write_bytes(metadata)
    except BaseException:
        if rebuild:
            with suppress(FileNotFoundError):
                shutil.rmtree(environment)
            if previous:
                previous.rename(environment)
                previous.parent.rmdir()
        raise
    if previous:
        print(f"Previous virtualenv saved in {previous}")
    print(f"Python wheels ready in {wheels}")
    print(f"Launcher ready: {shlex.quote(str(entry))}")


def install(project: Path, *, with_docker_deps: bool = False) -> None:
    if sys.platform != "linux" or not shutil.which("apt-get"):
        raise SetupError("loaf install requires Debian with APT (native or WSL2).")
    if not (project / PYTHON_PACKAGE_DIR / "pyproject.toml").is_file():
        raise SetupError(f"No Python package found in {project}.")
    if with_docker_deps:
        prepare_docker(project)
    print("Prepare the Python environment and wheel cache…", file=sys.stderr, flush=True)
    install_python(project)
    print("Prepare the NVIDIA Container Toolkit…", file=sys.stderr, flush=True)
    install_toolkit(project)
