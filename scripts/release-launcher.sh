#!/bin/bash
# loaf standalone backup launcher v1
set -euo pipefail

if [[ $(uname -sm) != "Linux x86_64" ]]; then
    echo "This loaf executable requires Linux on an amd64 CPU." >&2
    exit 1
fi

# LOAF_RUNTIME_LOCK
lock_loaf_runtime
runtime_parent="$loaf_data_home/loaf/runtime"
runtime="$runtime_parent/@BUNDLE_ID@"
if [[ -L "$runtime_parent" || -L "$runtime" ]]; then
    echo "loaf runtime cache must be a local directory." >&2
    exit 1
fi
if [[ ! -x "$runtime/python/bin/python3" ]]; then
    (
        umask 077
        mkdir -p -- "$runtime_parent"
        staging=$(mktemp -d "$runtime_parent/.unpack-XXXXXXXX")
        trap 'rm -rf -- "$staging"' EXIT
        start=$(awk '/^__LOAF_PAYLOAD__$/ { print NR + 1; exit }' "$0")
        tail -n "+$start" -- "$0" | tar -xzf - -C "$staging"
        mv -T --no-clobber -- "$staging" "$runtime"
    )
fi
if [[ $# == 1 && $1 == --license ]]; then
    exec cat -- "$runtime/LICENSE.txt"
fi
export LOAF_RELEASE_EXECUTABLE
LOAF_RELEASE_EXECUTABLE=$(readlink -f -- "$0")
exec "$runtime/python/bin/python3" -IB -m loaf "$@"
__LOAF_PAYLOAD__
