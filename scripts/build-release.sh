#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# TAG builds the release assets from the tagged commit. --binary-only and --debian-only build the standalone
# executable or the Debian package from the current working files.
mode=${1:?Use scripts/build-release.sh TAG [OUTPUT_DIRECTORY], --binary-only or --debian-only}
output=${2:-dist}
host_python=${LOAF_BUILD_PYTHON:-python3}
if [[ $mode != --binary-only && $mode != --debian-only ]]; then
    tag=$mode
    "$host_python" - "$tag" <(git show HEAD:python-package/pyproject.toml) <<'PY'
from pathlib import Path
import sys
import tomllib

metadata = tomllib.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
expected = "v" + metadata["project"]["version"]
if sys.argv[1] != expected:
    sys.exit(f"Expected tag {expected} for the package version, got {sys.argv[1]}.")
PY
fi

if [[ $(uname -sm) != "Linux x86_64" ]]; then
    echo "Build the standalone executable on Linux amd64." >&2
    exit 1
fi

source scripts/release-runtime.sh
lock_loaf_runtime
mkdir -p -- "$loaf_data_home/loaf"
stage=$(mktemp -d "$loaf_data_home/loaf/.build-XXXXXXXX")
pending=
trap 'rm -rf -- "$stage"; if [[ -n $pending ]]; then rm -rf -- "$pending"; fi' EXIT

run_logged() {
    local status
    if "$@" >"$stage/build.log" 2>&1; then
        return 0
    else
        status=$?
    fi
    cat "$stage/build.log" >&2
    return "$status"
}

bundle="$stage/bundle"
dist="$stage/dist"
mkdir -p "$bundle/wheels" "$bundle/python-licenses" "$dist"
if [[ $mode == --binary-only || $mode == --debian-only ]]; then
    source="$stage/source"
    mkdir -p "$source/python-package" "$source/scripts"
    # Follow the package's links to README.md and docs/ so that the wheel holds the guides.
    tar -chf - -C python-package --exclude=__pycache__ --exclude='*.egg-info' \
        pyproject.toml README.md LICENSE.txt THIRD_PARTY_NOTICES.txt src |
        tar -xf - -C "$source/python-package"
    cp -- scripts/release-launcher.sh scripts/release-runtime.sh "$source/scripts/"
else
    source_archive="$dist/loaf-$tag-source.tar.gz"
    git archive --format=tar.gz --prefix="loaf-$tag/" HEAD > "$source_archive"
    tar -xzf "$source_archive" -C "$stage"
    source="$stage/loaf-$tag"
fi

# Pin the interpreter and the notices for its compiled dependencies.
# install-cache/python/ keeps the checked downloads, so later builds and backups work offline.
python_release=20261110
"$host_python" - "$stage" "$python_release" "$PWD/install-cache/python" <<'PY'
from hashlib import sha256
from pathlib import Path
import shutil
import sys
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

stage, release, cache = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
asset = f"cpython-3.13.18+{release}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
downloads = (
    ("python.tar.gz", asset, f"https://github.com/astral-sh/python-build-standalone/releases/download/{release}/{asset}",
     "f2d2199eb25880e07863b391cf7124e7ffa1338b0d65600dfcec117c4223a7fb"),
    ("licenses.tar.gz", f"python-build-standalone-{release}.tar.gz",
     f"https://codeload.github.com/astral-sh/python-build-standalone/tar.gz/refs/tags/{release}",
     "63b8166347616283ee6d3ed449e957bc71b65368bc60945d21ab957e6fcaee49"),
)


def digest(path):
    value = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


for name, cached_name, url, expected in downloads:
    cached = cache / cached_name
    if cached.is_file() and not cached.is_symlink() and digest(cached) == expected:
        print(f"Reuse the cached {cached_name}.")
    else:
        print(f"Fetch {cached_name}…")
        cache.mkdir(parents=True, exist_ok=True)
        # Publish only a complete, checked download.
        with NamedTemporaryFile(dir=cache, prefix=".loaf-download-", delete=False) as output:
            pending = Path(output.name)
            try:
                with urlopen(url, timeout=60) as response:
                    shutil.copyfileobj(response, output)
            except BaseException:
                pending.unlink()
                raise
        if digest(pending) != expected:
            pending.unlink()
            sys.exit(f"SHA-256 mismatch for {url}.")
        pending.chmod(0o644)
        pending.replace(cached)
    shutil.copyfile(cached, stage / name)
PY
tar -xzf "$stage/python.tar.gz" -C "$bundle"
tar -xzf "$stage/licenses.tar.gz" -C "$bundle/python-licenses" --strip-components=1 --wildcards \
    '*/LICENSE' '*/LICENSE.*.txt'

python="$bundle/python/bin/python3"
version_tag=$("$python" -I - "$source/python-package/pyproject.toml" "$stage/build-requirements.txt" <<'PY'
from pathlib import Path
import sys
import tomllib

metadata = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
requirements = metadata["build-system"]["requires"]
Path(sys.argv[2]).write_text("\n".join(requirements) + "\n", encoding="utf-8")
print("v" + metadata["project"]["version"])
PY
)
pip=("$python" -Im pip --disable-pip-version-check --no-cache-dir --no-input)
local_pip() (
    for option in "${!PIP_@}"; do
        unset "$option"
    done
    export PIP_CONFIG_FILE=/dev/null
    "${pip[@]}" "$@"
)

requirements=(download --only-binary=:all: --dest "$bundle/wheels" --requirement "$stage/build-requirements.txt")
cache="$PWD/install-cache/wheels"
if [[ -d $cache ]] && local_pip "${requirements[@]}" --no-index --find-links "$cache" >/dev/null 2>&1; then
    echo "Reuse the cached Python build wheels."
else
    echo "Fetch Python build requirements…"
    run_logged "${pip[@]}" "${requirements[@]}"
fi

echo "Build the loaf wheel…"
run_logged local_pip wheel --no-index --no-deps --find-links "$bundle/wheels" --wheel-dir "$dist" "$source/python-package"

echo "Assemble the standalone executable…"
run_logged local_pip install --no-index --no-deps --no-compile "$dist/"*.whl
# The executable carries this wheel. The release wheel built below carries the executable instead.
rm -- "$dist/"*.whl
"$python" -I - "$source/python-package" "$bundle" "$stage/licenses.tar.gz" "$python_release" <<'PY'
from pathlib import Path
import json
import platform
import sys
import sysconfig
import tarfile
from zipfile import ZipFile

package, bundle, upstream = map(Path, sys.argv[1:4])
release = sys.argv[4]
with tarfile.open(upstream, "r:gz") as source:
    manifest, = (name for name in source.getnames() if name.endswith("/pythonbuild/downloads.json"))
    with source.extractfile(manifest) as metadata:
        downloads = json.load(metadata)
cpython = downloads[f"cpython-{sys.version_info.major}.{sys.version_info.minor}"]
if cpython["version"] != platform.python_version():
    sys.exit("The bundled Python version differs from the pinned source manifest.")
bdb = downloads["bdb"]
licenses = bundle / "python-licenses"
berkeley_license = licenses / bdb["license_file"]
if not berkeley_license.stat().st_size:
    sys.exit("The pinned Berkeley DB license is empty.")

with (bundle / "LICENSE.txt").open("wb") as output:
    seen = set()

    def append_license(title, text):
        if text in seen:
            return
        seen.add(text)
        output.write(f"\n\n--- {title} ---\n".encode("utf-8"))
        output.write(text)

    project_license = (package / "LICENSE.txt").read_bytes()
    output.write(project_license)
    seen.add(project_license)
    append_license("Third-party notices", (package / "THIRD_PARTY_NOTICES.txt").read_bytes())
    source_locations = (
        f"Berkeley DB {bdb['version']}, linked into CPython's _dbm extension (dbm.ndbm).\n"
        f"  {bdb['url']}\n  SHA-256 {bdb['sha256']}\n\n"
        f"CPython {cpython['version']}, including the _dbm extension source.\n"
        f"  {cpython['url']}\n  SHA-256 {cpython['sha256']}\n\n"
        f"Python Build Standalone {release} patches, build scripts and complete input manifest.\n"
        f"  https://codeload.github.com/astral-sh/python-build-standalone/tar.gz/refs/tags/{release}\n"
        "  The archive's pythonbuild/downloads.json lists the other pinned source inputs.\n\n"
        f"pip {downloads['pip']['version']} source, including its vendored components.\n"
        f"  https://pypi.org/project/pip/{downloads['pip']['version']}/#files\n"
    )
    append_license("Source locations for the bundled runtime", source_locations.encode("utf-8"))
    append_license("Berkeley DB license", berkeley_license.read_bytes())
    stdlib = Path(sysconfig.get_path("stdlib"))
    append_license("Bundled CPython license", (stdlib / "LICENSE.txt").read_bytes())
    for notice in sorted(licenses.glob("LICENSE*")):
        append_license(f"Python Build Standalone / {notice.name}", notice.read_bytes())
    site = Path(sysconfig.get_path("purelib"))
    for notice in sorted(site.glob("*.dist-info/licenses/**/*")):
        if notice.is_file():
            append_license(str(notice.relative_to(site)), notice.read_bytes())
    for wheel in sorted((bundle / "wheels").glob("*.whl")):
        with ZipFile(wheel) as archive:
            for name in sorted(archive.namelist()):
                if ".dist-info/licenses/" in name and not name.endswith("/"):
                    append_license(f"{wheel.name} / {name}", archive.read(name))
PY
cp -- "$bundle/LICENSE.txt" "$dist/LICENSE.txt"

# Leave out what loaf never runs. docs/releases.md#executable-size explains each part and what stays.
(
    cd "$bundle/python"
    # bin/python3.13 links libpython statically, so the shared library is a second copy.
    rm -rf -- lib/libpython3*.so* include share lib/pkgconfig bin/python3*-config bin/idle3*
    # Tcl/Tk, IDLE and turtle graphics need a desktop session.
    rm -rf -- lib/tcl[0-9]* lib/tk[0-9]* lib/itcl* lib/thread[0-9]* lib/libtcl* lib/libtk* \
        lib/python3.*/tkinter lib/python3.*/idlelib lib/python3.*/turtledemo lib/python3.*/turtle.py \
        lib/python3.*/lib-dynload/_tkinter.*
    # Only virtualenvs with pip need ensurepip; the launcher creates none with this runtime.
    rm -rf -- lib/python3.*/ensurepip
)
run_logged "$python" -I -c 'import loaf.cli, unittest, sqlite3, ssl, venv, urllib.request, pydoc'
run_logged "$python" -I -m pip --version
printf '%s\n' "$version_tag" > "$bundle/.loaf-release"
tar -cf - -C "$bundle" .loaf-release LICENSE.txt python python-licenses wheels | gzip -9n > "$stage/payload.tar.gz"
bundle_id=$(sha256sum "$stage/payload.tar.gz" | cut -d ' ' -f 1)
"$host_python" - "$source/scripts" "$bundle_id" > "$dist/loaf" <<'PY'
from pathlib import Path
import sys

scripts = Path(sys.argv[1])
launcher = (scripts / "release-launcher.sh").read_text(encoding="utf-8")
marker = "# LOAF_RUNTIME_LOCK"
if marker in launcher:
    launcher = launcher.replace(marker, (scripts / "release-runtime.sh").read_text(encoding="utf-8").rstrip())
sys.stdout.write(launcher.replace("@BUNDLE_ID@", sys.argv[2]))
PY
cat "$stage/payload.tar.gz" >> "$dist/loaf"
chmod 755 "$dist/loaf"

echo "Check the standalone executable…"
mkdir "$stage/project"
run_logged bash -eu -c '
    cd "$1"
    export XDG_DATA_HOME="$2"
    launcher="$3"
    "$launcher" setup --dry-run
    "$launcher" setup
    test -f config/config.ini
    # Setup copies the model list in effect, here the packaged one.
    test -f config/models.ini
    test -f docs/README.md
    test -f docs/.loaf-docs
    "$launcher" version | grep -q "^Installation: standalone executable "
    "$launcher" docs readme | grep -q "^# Local AI Ferry"
    test ! -e .venv
    test ! -e .git
    test ! -e python-package
    "$launcher" download --dry-run
    "$launcher" run offline --cpu --dry-run
    "$launcher" backup config --dry-run | grep -q "^Include the standalone executable $launcher$"
    "$launcher" --license | cmp - "$4"
' bash "$stage/project" "$stage/data" "$dist/loaf" "$dist/LICENSE.txt"
mkdir -p -- "$output"
if [[ $mode == --binary-only ]]; then
    pending=$(mktemp -d "$output/.loaf-binary-XXXXXXXX")
    install -m 644 -- "$dist/LICENSE.txt" "$pending/LICENSE.txt"
    install -m 755 -- "$dist/loaf" "$pending/loaf"
    mv -fT -- "$pending/LICENSE.txt" "$output/LICENSE.txt"
    mv -fT -- "$pending/loaf" "$output/loaf"
    printf 'Standalone executable ready at %s\n' "$output/loaf"
    exit 0
fi

# The release wheel and the Debian package carry the standalone executable, so that backups from either
# installation include it.
echo "Build the release wheel with the standalone executable…"
install -D -m 755 -- "$dist/loaf" "$source/python-package/src/loaf/data/standalone/loaf"
run_logged local_pip wheel --no-index --no-deps --find-links "$bundle/wheels" --wheel-dir "$dist" "$source/python-package"
echo "Build the Debian package…"
if [[ $mode == --debian-only ]]; then
    epoch=$(git log -1 --format=%ct HEAD 2>/dev/null || date +%s)
else
    epoch=$(git log -1 --format=%ct HEAD)
fi
# The tagged source archive holds the packaging files; working-file builds use the checkout's own.
deb_script=$source/scripts/build-deb.sh
[[ $mode == --debian-only ]] && deb_script=scripts/build-deb.sh
run_logged env SOURCE_DATE_EPOCH="$epoch" LOAF_BUILD_PYTHON="$host_python" bash "$deb_script" "$dist/"*.whl "$dist"
if [[ $mode == --debian-only ]]; then
    package=$(basename -- "$dist/"*.deb)
    pending=$(mktemp -d "$output/.loaf-binary-XXXXXXXX")
    install -m 644 -- "$dist/$package" "$pending/$package"
    mv -fT -- "$pending/$package" "$output/$package"
    printf 'Debian package ready at %s\n' "$output/$package"
    exit 0
fi
(
    cd "$dist"
    sha256sum -- loaf LICENSE.txt *.whl *.deb *.tar.gz > SHA256SUMS
)
cp -- "$dist/loaf" "$dist/LICENSE.txt" "$dist/"*.whl "$dist/"*.deb "$source_archive" "$dist/SHA256SUMS" "$output/"
printf 'Release assets ready at %s\n' "$output"
