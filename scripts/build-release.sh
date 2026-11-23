#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

mode=${1:?Use scripts/build-release.sh TAG [OUTPUT_DIRECTORY] or --binary-only}
output=${2:-dist}
host_python=${LOAF_BUILD_PYTHON:-python3}
if [[ $mode != --binary-only ]]; then
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
if [[ $mode == --binary-only ]]; then
    source="$stage/source"
    mkdir -p "$source/python-package" "$source/scripts"
    tar -cf - -C python-package --exclude=__pycache__ --exclude='*.egg-info' \
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
python_release=20261110
echo "Fetch the standalone Python runtime…"
"$host_python" - "$stage" "$python_release" <<'PY'
from hashlib import sha256
from pathlib import Path
import shutil
import sys
from urllib.request import urlopen

release = sys.argv[2]
asset = f"cpython-3.13.18+{release}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
downloads = (
    ("python.tar.gz", f"https://github.com/astral-sh/python-build-standalone/releases/download/{release}/{asset}",
     "f2d2199eb25880e07863b391cf7124e7ffa1338b0d65600dfcec117c4223a7fb"),
    ("licenses.tar.gz", f"https://codeload.github.com/astral-sh/python-build-standalone/tar.gz/refs/tags/{release}",
     "63b8166347616283ee6d3ed449e957bc71b65368bc60945d21ab957e6fcaee49"),
)
for name, url, expected in downloads:
    path = Path(sys.argv[1]) / name
    with urlopen(url, timeout=60) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)
    with path.open("rb") as download:
        digest = sha256()
        for block in iter(lambda: download.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        sys.exit(f"SHA-256 mismatch for {url}.")
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
printf '%s\n' "$version_tag" > "$bundle/.loaf-release"
tar -czf "$stage/payload.tar.gz" -C "$bundle" .loaf-release LICENSE.txt python python-licenses wheels
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
    test -f config/models.ini
    test ! -e .venv
    test ! -e .git
    test ! -e python-package
    "$launcher" download --dry-run
    "$launcher" offline --cpu --dry-run
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
else
    (
        cd "$dist"
        sha256sum -- loaf LICENSE.txt *.whl *.tar.gz > SHA256SUMS
    )
    cp -- "$dist/loaf" "$dist/LICENSE.txt" "$dist/"*.whl "$source_archive" "$dist/SHA256SUMS" "$output/"
    printf 'Release assets ready at %s\n' "$output"
fi
