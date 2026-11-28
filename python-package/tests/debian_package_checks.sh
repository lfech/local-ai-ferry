#!/bin/sh
# Install the loaf Debian package in a clean Debian container and check the system deployment files.
# Docker itself is outside this check, so the package is installed without its Docker dependencies.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 groff-base >/dev/null
dpkg --force-depends -i /deb/loaf.deb >/dev/null 2>&1 || dpkg --force-depends -i /deb/loaf.deb

fail() { echo "FAIL: $*" >&2; exit 1; }
loaf version | grep -q '^Installation: Debian package$' || fail "version does not report the Debian package"
loaf version | grep -q '^Default deployment: system deployment (/etc/loaf, /var/lib/loaf)$' || fail "default deployment"
loaf docs | grep -q '^  installation ' || fail "guide list"
loaf docs readme | head -1 | grep -q '^# Local AI Ferry' || fail "README guide"
[ "$(stat -c %a:%U /etc/loaf)" = 750:root ] || fail "/etc/loaf permissions"
[ "$(stat -c %a:%U /var/lib/loaf)" = 2770:root ] || fail "/var/lib/loaf permissions"
[ "$(stat -c %a /var/lib/loaf/backup)" = 2770 ] || fail "shared backup directory permissions"
[ "$(stat -c %a /etc/loaf/terminal-server.key)" = 640 ] || fail "terminal key permissions"
grep -qx 'secret_file = /etc/loaf/terminal-server.key' /etc/loaf/config.ini || fail "system key setting"
grep -qx '# ollama_image = loaf-ollama-cuda13' /etc/loaf/config.ini || fail "commented defaults"
loaf --system setup terminal-server-key --dry-run | grep -q '/etc/loaf/terminal-server.key' || fail "key setup preview"
# Backups of the system deployment carry the packaged standalone executable.
standalone=/usr/lib/python3/dist-packages/loaf/data/standalone/loaf
[ -x "$standalone" ] || fail "standalone executable"
loaf --system backup config --dry-run | grep -qx "Include the standalone executable $standalone" || fail "backup preview"
if [ "$(uname -m)" = x86_64 ]; then
    XDG_DATA_HOME=/tmp/standalone "$standalone" version | grep -q '^Installation: standalone executable ' \
        || fail "packaged standalone executable"
fi
# Slim images exclude /usr/share/man, so read the manual page from the package itself.
dpkg-deb --fsys-tarfile /deb/loaf.deb | tar -xOf - ./usr/share/man/man1/loaf.1.gz > /tmp/loaf.1.gz
gzip -dc /tmp/loaf.1.gz > /tmp/loaf.1
grep -q '^\.SH COMMANDS$' /tmp/loaf.1 || fail "manual page content"
groff -man -Tutf8 -ww -z /tmp/loaf.1 2>/tmp/manual-warnings
[ ! -s /tmp/manual-warnings ] || { cat /tmp/manual-warnings >&2; fail "manual page warnings"; }
[ -n "$(find /usr/lib/python3/dist-packages/loaf -name '*.pyc' -print -quit)" ] || fail "py3compile did not run"
completions=/usr/share/bash-completion/completions/loaf
head -1 "$completions" | grep -q 'removed with the Debian package' || fail "system Bash completion"
bash -c '. "$1"; compopt() { :; }; COMP_WORDS=(loaf dock); COMP_CWORD=1; _loaf; [ "${COMPREPLY[*]}" = docker ]' \
    bash "$completions" || fail "Bash completion words"
loaf completion install | grep -q 'Debian package provides Bash completion' || fail "completion install in system mode"
[ ! -e "$HOME/.local/share/bash-completion/completions/loaf" ] || fail "user completion written beside the package"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "Python version"

touch /var/lib/loaf/backup/kept
dpkg --purge loaf >/dev/null
[ ! -e /usr/bin/loaf ] || fail "launcher remains after purge"
[ ! -e /usr/share/bash-completion/completions/loaf ] || fail "Bash completion remains after purge"
[ ! -e /usr/lib/python3/dist-packages/loaf ] || fail "package files or bytecode remain after purge"
[ ! -e /etc/loaf ] || fail "/etc/loaf remains after purge"
[ -e /var/lib/loaf/backup/kept ] || fail "purge removed a backup"
echo "Debian package checks passed on $(. /etc/os-release && echo "$PRETTY_NAME") with Python $(python3 -c 'import platform; print(platform.python_version())')."
