#!/usr/bin/env bash
set -euo pipefail

# Run in a disposable container with test volumes mounted under /transfer.
[[ "${DISPOSABLE_ARCHIVE_TEST:-}" == 1 ]] || { echo 'Use the unittest Docker runner.' >&2; exit 2; }
case "${1:-}" in
    seed) root=/transfer ;;
    verify) root=$(mktemp -d) ;;
    *) exit 2 ;;
esac

model="$root/ollama/config/hf.co/owner/repo:Q4_K_M.model"
mkdir -p "$(dirname "$model")" "$root/ollama/models/blobs" "$root/webui"
printf 'FROM /ollama/models/blobs/weights\n' > "$model"
printf 'model-fixture\000binary\377' > "$root/ollama/models/blobs/weights"
printf '{"num_ctx":16384}' > "$root/webui/settings.json"
printf 'SQLite format 3\000fixture\377' > "$root/webui/webui.db"

if [[ "$1" == verify ]]; then
    diff -r "$root/ollama" /transfer/ollama
    diff -r "$root/webui" /transfer/webui
fi
