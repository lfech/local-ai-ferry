#!/usr/bin/env bash
# start-ollama-offline.sh - PID 1 wrapper for local server startup and registration.
# Import exported definitions only after the API responds; advertise readiness
# only after every import succeeds. No model download is attempted here.

# --- Validate configuration before creating a child process ---
set -euo pipefail

# A container restart can retain /tmp.
ready_file=/tmp/ollama-imports-complete
rm -f -- "$ready_file"

# Reject leading zeroes as well: Bash arithmetic otherwise treats them as octal.
startup_timeout=${OLLAMA_STARTUP_TIMEOUT:-60}
if [[ ! "$startup_timeout" =~ ^[1-9][0-9]*$ ]]; then
    printf 'OLLAMA_STARTUP_TIMEOUT must be a positive integer.\n' >&2
    exit 2
fi

mkdir -p /ollama/models /ollama/config

# --- Supervise Ollama and forward container shutdown through EXIT cleanup ---
ollama serve &
server_pid=$!

cleanup() {
    rm -f -- "$ready_file" || true
    # The child may already have exited. Reap it either way without masking the
    # wrapper's original error or signal status with a cleanup failure.
    kill -TERM "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT
# 143 = 128 + SIGTERM; 130 = 128 + SIGINT. Both paths also run EXIT cleanup.
trap 'exit 143' TERM
trap 'exit 130' INT

# Use loopback for CLI calls while the server listens on all interfaces.
ollama_local() {
    OLLAMA_HOST=127.0.0.1:11434 ollama "$@"
}

# --- Wait for a live API, with a bounded startup deadline ---
ready=false
deadline=$((SECONDS + startup_timeout))
while (( (remaining = deadline - SECONDS) > 0 )); do
    # Signal zero probes existence without sending a signal; a dead child should
    # fail immediately instead of waiting out the entire API readiness timeout.
    if ! kill -0 "$server_pid" 2>/dev/null; then
        printf 'Ollama exited before becoming ready.\n' >&2
        exit 1
    fi
    # Bound an individual request as well as the retry loop.
    probe_timeout=$((remaining < 2 ? remaining : 2))
    if OLLAMA_HOST=127.0.0.1:11434 timeout --kill-after=1s "${probe_timeout}s" ollama list >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [[ "$ready" != true ]]; then
    printf 'Ollama did not become ready within %s seconds.\n' "$startup_timeout" >&2
    exit 1
fi

# --- Recreate registrations from exported, recursively namespaced Modelfiles ---
# globstar makes ** recursive; nullglob gives an empty array when nothing matches
# instead of trying to import a file literally named /ollama/config/**/*.model.
shopt -s globstar nullglob
modelfiles=(/ollama/config/**/*.model)
for modelfile in "${modelfiles[@]}"; do
    [[ -f "$modelfile" ]] || continue
    # Strip only the fixed prefix and final suffix. Namespace directories and
    # quantization tags remain part of the registered model's original name.
    model=${modelfile#/ollama/config/}
    model=${model%.model}
    printf 'Importing %s from %s\n' "$model" "$modelfile"
    # Exported FROM/ADAPTER paths still point to /ollama/models/blobs/...
    if ! ollama_local create --file "$modelfile" -- "$model"; then
        printf 'Import failed for %s. Check the Modelfile and model blobs.\n' "$model" >&2
        exit 1
    fi
done

if (( ${#modelfiles[@]} == 0 )); then
    printf 'No .model files found; serving any existing model manifests.\n'
fi
# --- Publish health only after imports, then keep PID 1 tied to the server ---
touch "$ready_file"
printf 'Ollama is ready for inference.\n'
# Preserve an unexpected server exit code; EXIT still handles final cleanup.
wait "$server_pid"
