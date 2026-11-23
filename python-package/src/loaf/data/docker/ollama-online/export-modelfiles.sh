#!/usr/bin/env bash
# export-modelfiles.sh - Export cached Ollama definitions into /ollama/config.
# Run inside the online container after pulling models; this helper never pulls.
# Keep the generated text intact, including templates and multimodal FROM lines.

# --- Local client and model selection ---
set -euo pipefail
export OLLAMA_HOST=127.0.0.1:11434

# With no arguments, export every locally registered model. Use explicit tags
# (e.g. llama3.2:3b) so that the imported model keeps the same name.
if (( $# == 0 )); then
    # Capture separately: a failure inside < <(...) would not propagate through
    # mapfile under set -e. Arrays also preserve each model as one CLI argument.
    model_list=$(ollama list)
    mapfile -t models < <(printf '%s\n' "$model_list" | awk 'NR > 1 {print $1}')
else
    models=("$@")
fi

# --- Interrupted/failed exports must leave the previous complete file intact ---
temporary_file=
cleanup() {
    if [[ -n "$temporary_file" ]]; then
        rm -f -- "$temporary_file"
    fi
}
trap cleanup EXIT
# Conventional 128 + signal statuses; exiting also invokes the cleanup trap.
trap 'exit 143' TERM
trap 'exit 130' INT

# --- Validate every destination before writing any file ---
# Validate the whole argument list before creating any directories or exports.
for model in "${models[@]}"; do
    if [[ ! "$model" =~ ^[A-Za-z0-9][A-Za-z0-9_.:/-]*$ ]]; then
        printf 'Invalid model name: %s\n' "$model" >&2
        exit 1
    fi
    # The character whitelist permits namespaces, but path components must not
    # escape /ollama/config or collapse into a different destination.
    case "$model" in
        */|*//*|*/.|*/..|*/../*|*/./*)
            printf 'Invalid model name: %s\n' "$model" >&2
            exit 1
            ;;
    esac
done

# --- Export to a sibling temporary file, then publish atomically ---
for model in "${models[@]}"; do
    # A model namespace becomes a directory; keep colons/tags inside the Linux
    # named volume, where they are valid even when Docker runs on Windows.
    destination="/ollama/config/${model}.model"
    mkdir -p -- "$(dirname -- "$destination")"
    # Same-directory rename stays on the same filesystem. A failed `show` cannot
    # replace a good export with a partial file; EXIT removes its temporary file.
    temporary_file=$(mktemp "${destination}.tmp.XXXXXX")
    ollama show --modelfile "$model" > "$temporary_file"
    mv -fT -- "$temporary_file" "$destination"
    temporary_file=
    printf 'Exported %s -> %s\n' "$model" "$destination"
done

if (( ${#models[@]} == 0 )); then
    printf 'No local models found. Run ollama pull first.\n'
fi
