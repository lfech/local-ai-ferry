#!/usr/bin/env bash
# Build the loaf Debian package from a built wheel: scripts/build-deb.sh WHEEL [OUTPUT_DIRECTORY]
set -euo pipefail
wheel=$(readlink -f -- "${1:?Use scripts/build-deb.sh WHEEL [OUTPUT_DIRECTORY]}")
output=$(mkdir -p -- "${2:-dist}" && readlink -f -- "${2:-dist}")
cd "$(dirname "$0")/.."
host_python=${LOAF_BUILD_PYTHON:-python3}
maintainer=${LOAF_DEB_MAINTAINER:-Local AI Ferry maintainers <maintainers@local-ai-ferry.invalid>}
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
root="$stage/root"
site="$root/usr/lib/python3/dist-packages"
mkdir -p "$site" "$root/DEBIAN" "$root/usr/bin" "$root/etc/loaf" "$root/usr/share/doc/loaf" \
    "$root/usr/share/man/man1" "$root/usr/share/bash-completion/completions" "$root/usr/lib/systemd/system"

# Install the wheel as a Debian system package and generate the system files from it.
"$host_python" -I - "$wheel" "$site" "$root" <<'PY'
import gzip
import sys
import zipfile
from pathlib import Path

wheel, site, root = map(Path, sys.argv[1:])
with zipfile.ZipFile(wheel) as archive:
    archive.extractall(site)
info, = site.glob("*.dist-info")
(info / "RECORD").unlink()
# /usr/share/doc/loaf/copyright carries the license and notices.
for license in sorted((info / "licenses").iterdir()):
    license.unlink()
(info / "licenses").rmdir()
(info / "INSTALLER").write_text("debian\n", encoding="utf-8")
(site / "loaf/data/debian-package").write_text("Installed by the loaf Debian package.\n", encoding="utf-8")
sys.dont_write_bytecode = True
sys.path.insert(0, str(site))
from loaf import completion, docs, setup

secret = {("terminal_server", "secret_file"): "/etc/loaf/terminal-server.key"}
(root / "etc/loaf/config.ini").write_text(setup.config_template(setup.SYSTEM_CONFIG_HEADER, secret), encoding="utf-8")
with gzip.GzipFile(root / "usr/share/man/man1/loaf.1.gz", "wb", 9, mtime=0) as manual:
    manual.write(docs.manual_page().encode("utf-8"))
# bash-completion loads it on first use; dpkg removes it with the package.
(root / "usr/share/bash-completion/completions/loaf").write_text(completion.script(system=True), encoding="utf-8")
metadata = (info / "METADATA").read_text(encoding="utf-8")
fields = dict(line.split(": ", 1) for line in metadata.split("\n\n", 1)[0].splitlines() if ": " in line)
homepage = next(entry.split(", ", 1)[1] for entry in metadata.splitlines()
                if entry.startswith("Project-URL: Repository, "))
(root.parent / "version").write_text(fields["Version"], encoding="utf-8")
(root.parent / "homepage").write_text(homepage, encoding="utf-8")
PY
version=$(cat "$stage/version")
# Debian packages ship sources; Python compiles them as needed.
find "$site" -name __pycache__ -prune -exec rm -rf {} +
homepage=$(cat "$stage/homepage")

printf '#!/bin/sh\n# Isolated mode keeps the working directory and user site out of the module path.\nexec /usr/bin/python3 -I -m loaf "$@"\n' \
    > "$root/usr/bin/loaf"
install -m 644 packaging/debian/loaf.service "$root/usr/lib/systemd/system/loaf.service"
install -m 644 packaging/debian/README.Debian "$root/usr/share/doc/loaf/README.Debian"
install -m 644 THIRD_PARTY_NOTICES.txt "$root/usr/share/doc/loaf/THIRD_PARTY_NOTICES.txt"
install -d "$root/usr/share/lintian/overrides"
install -m 644 packaging/debian/lintian-overrides "$root/usr/share/lintian/overrides/loaf"
{
    printf 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n'
    printf 'Upstream-Name: Local AI Ferry\nSource: %s\n\nFiles: *\n' "$homepage"
    printf 'Copyright: %s\n' "$(sed -n '1s/^Copyright (c) //p' LICENSE.txt)"
    printf 'License: BSD-3-Clause\n'
    sed -n '3,$p' LICENSE.txt | sed -e 's/^$/./' -e 's/^/ /'
} > "$root/usr/share/doc/loaf/copyright"
{
    printf 'loaf (%s) unstable; urgency=medium\n\n' "$version"
    printf '  * Release %s.\n  * Release notes: %s/releases\n\n' "$version" "$homepage"
    printf ' -- %s  %s\n' "$maintainer" "$(date -u -R -d "@$SOURCE_DATE_EPOCH")"
} | gzip -9n > "$root/usr/share/doc/loaf/changelog.gz"

install -m 755 packaging/debian/postinst packaging/debian/prerm packaging/debian/postrm "$root/DEBIAN/"
printf '/etc/loaf/config.ini\n' > "$root/DEBIAN/conffiles"
find "$root" -path "$root/DEBIAN" -prune -o -type d -exec chmod 755 {} +
find "$root" -path "$root/DEBIAN" -prune -o -type f -exec chmod 644 {} +
chmod 755 "$root/usr/bin/loaf"
find "$site/loaf/data/docker" -name "*.sh" -exec chmod 755 {} +
# The standalone executable from the release wheel, which backups of the system deployment include.
if [[ -f "$site/loaf/data/standalone/loaf" ]]; then
    chmod 755 "$site/loaf/data/standalone/loaf"
fi
(cd "$root" && find . -path ./DEBIAN -prune -o -path ./etc -prune -o -type f -printf '%P\0' | sort -z |
    xargs -0 md5sum) > "$root/DEBIAN/md5sums"
installed_size=$(du -sk --exclude=DEBIAN "$root" | cut -f1)
sed -e "s|@VERSION@|$version|" -e "s|@MAINTAINER@|$maintainer|" -e "s|@INSTALLED_SIZE@|$installed_size|" \
    -e "s|@HOMEPAGE@|$homepage|" packaging/debian/control.in > "$root/DEBIAN/control"
find "$root" -exec touch --no-dereference -d "@$SOURCE_DATE_EPOCH" {} +
package="$output/loaf_${version}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$root" "$package" >/dev/null
if command -v lintian >/dev/null; then
    lintian --profile debian --fail-on error "$package"
fi
printf 'Debian package ready at %s\n' "$package"
