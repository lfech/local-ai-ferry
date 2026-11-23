#!/usr/bin/env bash
# script_checks.sh - Exercise real helpers against a controllable fake Ollama CLI.
# Run only inside the disposable Docker container created by test_docker.py.
# /ollama and /tmp here are throwaway container paths, never the user's volume.

# --- Isolated helper copies and mock CLI installation ---
set -euo pipefail
[[ "${DISPOSABLE_SCRIPT_TEST:-}" == 1 ]] || { echo 'Use the unittest Docker runner.' >&2; exit 2; }
sed 's/\r$//' /export.sh > /tmp/export-modelfiles
sed 's/\r$//' /start.sh > /tmp/start-ollama-offline
chmod +x /tmp/export-modelfiles /tmp/start-ollama-offline
mkdir -p /tmp/mockbin /ollama/models
# PATH interception runs production control flow without networking or real models.
export PATH="/tmp/mockbin:$PATH"

# Quoted delimiter defers all $ expansion until the generated mock is executed.
cat > /tmp/mockbin/ollama <<'MOCK'
#!/usr/bin/env bash
# Fixture CLI: marker files expose lifecycle state to the outer test harness.
set -eu
case "$1" in
    serve)
        # Fail before readiness, or remain alive until the test requests shutdown.
        [[ "${FAKE_SERVER_DIE:-0}" != 1 ]] || exit 9
        echo "$$" > "$FAKE_STATE/server-pid"
        touch "$FAKE_STATE/running"
        trap 'rm -f "$FAKE_STATE/running"; exit 0' TERM INT
        while [[ ! -f "$FAKE_STATE/stop" ]]; do sleep 0.05; done
        rm -f "$FAKE_STATE/running"
        exit "${FAKE_SERVER_EXIT:-0}"
        ;;
    list)
        # API availability and an empty catalog are independent conditions.
        if [[ "${FAKE_LIST_HANG:-0}" == 1 ]]; then sleep 30; fi
        [[ "${FAKE_LIST_FAIL:-0}" != 1 ]] || exit 5
        if [[ "${FAKE_REQUIRE_SERVER:-0}" == 1 && ! -f "$FAKE_STATE/running" ]]; then exit 1; fi
        printf 'NAME ID SIZE MODIFIED\n'
        if [[ "${FAKE_EMPTY:-0}" != 1 ]]; then
            printf 'sample:latest abc 1GB now\norg/assistant:tag def 1GB now\n'
        fi
        ;;
    show)
        # Partial output followed by failure catches non-atomic export overwrites.
        if [[ "${FAKE_SHOW_FAIL:-0}" == 1 ]]; then printf 'partial'; exit 7; fi
        printf 'FROM /ollama/models/blobs/weights\nFROM /ollama/models/blobs/projector\n'
        ;;
    create)
        # Record name and file as tab-separated fields for exact path assertions.
        [[ "$2" == --file && "$4" == -- ]]
        printf '%s\t%s\n' "$5" "$3" >> "$FAKE_STATE/imports"
        [[ "${FAKE_FAIL_IMPORT:-}" != "$5" ]] || exit 8
        ;;
    *) exit 90 ;;
esac
MOCK
chmod +x /tmp/mockbin/ollama

# Each invocation runs one case in a fresh container.
mkdir -p /ollama/config
FAKE_STATE=$(mktemp -d)
export FAKE_STATE
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
trap 'printf "Fixture failed on line %d: %s\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

wait_ready() {
    # 100 * 50 ms bounds the fixture wait to about five seconds. kill -0 checks
    # liveness without sending a signal, avoiding waits for an already dead child.
    for (( poll=0; poll<100; poll++ )); do
        [[ ! -f /tmp/ollama-imports-complete ]] || return 0
        kill -0 "$launcher_pid" 2>/dev/null || { cat "$FAKE_STATE/log"; fail 'launcher exited early'; }
        sleep 0.05
    done
    fail 'readiness deadline exceeded'
}
assert_server_stopped() {
    # A wrapper's exit alone does not prove it terminated/reaped its server child.
    if [[ -f "$FAKE_STATE/server-pid" ]]; then
        ! kill -0 "$(cat "$FAKE_STATE/server-pid")" 2>/dev/null || fail 'orphaned server'
    fi
}

case "${1:-}" in
    export-all)
        /tmp/export-modelfiles >/dev/null
        test -f /ollama/config/sample:latest.model
        test -f /ollama/config/org/assistant:tag.model
        test "$(grep -c '^FROM ' /ollama/config/sample:latest.model)" = 2
        ;;
    export-atomic)
        /tmp/export-modelfiles sample:latest >/dev/null
        # Expected failures live in if/|| contexts so set -e does not abort the harness.
        export FAKE_SHOW_FAIL=1
        if /tmp/export-modelfiles sample:latest >/dev/null 2>&1; then fail 'failed show accepted'; fi
        test "$(grep -c '^FROM ' /ollama/config/sample:latest.model)" = 2
        ! compgen -G '/ollama/config/*.tmp.*' >/dev/null || fail 'temporary export left behind'
        ;;
    export-directory)
        mkdir /ollama/config/sample:latest.model
        if /tmp/export-modelfiles sample:latest >/dev/null 2>&1; then fail 'export accepted directory as destination'; fi
        test -z "$(find /ollama/config -type f -print)"
        ;;
    export-invalid-input)
        # Validate the entire selection before exporting even its first valid item.
        for invalid in '../escape' '/absolute' 'a/../escape' '--help' 'a//b' 'a/' 'a b' 'a\b'; do
            if /tmp/export-modelfiles valid:latest "$invalid" >/dev/null 2>&1; then fail "accepted $invalid"; fi
        done
        test ! -f /ollama/config/valid:latest.model
        ;;
    export-unavailable-api)
        export FAKE_LIST_FAIL=1
        if /tmp/export-modelfiles >/dev/null 2>&1; then fail 'unavailable API accepted'; fi
        test -z "$(find /ollama/config -type f -print)"
        ;;
    export-empty)
        export FAKE_EMPTY=1
        /tmp/export-modelfiles | grep -q 'No local models'
        ;;
    startup-import)
        /tmp/export-modelfiles >/dev/null
        export FAKE_REQUIRE_SERVER=1
        /tmp/start-ollama-offline > "$FAKE_STATE/log" 2>&1 & launcher_pid=$!
        wait_ready
        test "$(wc -l < "$FAKE_STATE/imports")" = 2
        # Bash $'...' quoting turns \t into the literal field separator recorded by create.
        grep -q $'^org/assistant:tag\t/ollama/config/org/assistant:tag.model$' "$FAKE_STATE/imports"
        touch "$FAKE_STATE/stop"
        wait "$launcher_pid"
        assert_server_stopped
        ;;
    startup-signal)
        export FAKE_REQUIRE_SERVER=1
        /tmp/start-ollama-offline > "$FAKE_STATE/log" 2>&1 & launcher_pid=$!
        wait_ready
        test ! -f "$FAKE_STATE/imports"
        kill -TERM "$launcher_pid"
        status=0
        wait "$launcher_pid" || status=$?
        test "$status" = 143
        assert_server_stopped
        ;;
    startup-import-failure)
        /tmp/export-modelfiles >/dev/null
        export FAKE_REQUIRE_SERVER=1 FAKE_FAIL_IMPORT=org/assistant:tag
        touch /tmp/ollama-imports-complete
        if /tmp/start-ollama-offline > "$FAKE_STATE/log" 2>&1; then fail 'failed import accepted'; fi
        test ! -f /tmp/ollama-imports-complete
        assert_server_stopped
        ;;
    startup-server-death)
        export FAKE_REQUIRE_SERVER=1 FAKE_SERVER_DIE=1
        if /tmp/start-ollama-offline > "$FAKE_STATE/log" 2>&1; then fail 'dead server accepted'; fi
        test ! -f /tmp/ollama-imports-complete
        ;;
    startup-timeout)
        export FAKE_REQUIRE_SERVER=1 FAKE_LIST_FAIL=1 OLLAMA_STARTUP_TIMEOUT=1
        if /tmp/start-ollama-offline > "$FAKE_STATE/log" 2>&1; then fail 'unready server accepted'; fi
        test ! -f /tmp/ollama-imports-complete
        assert_server_stopped
        ;;
    startup-hung-api)
        export FAKE_LIST_HANG=1 OLLAMA_STARTUP_TIMEOUT=1
        status=0
        timeout 5 /tmp/start-ollama-offline > "$FAKE_STATE/log" 2>&1 || status=$?
        test "$status" = 1
        grep -q 'did not become ready' "$FAKE_STATE/log"
        assert_server_stopped
        ;;
    startup-invalid-timeout)
        export OLLAMA_STARTUP_TIMEOUT=bad
        if /tmp/start-ollama-offline > "$FAKE_STATE/log" 2>&1; then fail 'invalid timeout accepted'; fi
        test ! -f "$FAKE_STATE/server-pid"
        ;;
    startup-server-error)
        export FAKE_REQUIRE_SERVER=1 FAKE_SERVER_EXIT=7
        /tmp/start-ollama-offline > "$FAKE_STATE/log" 2>&1 & launcher_pid=$!
        wait_ready
        touch "$FAKE_STATE/stop"
        status=0
        wait "$launcher_pid" || status=$?
        test "$status" = 7
        test ! -f /tmp/ollama-imports-complete
        assert_server_stopped
        ;;
    *) fail "unknown fixture case ${1:-<missing>}" ;;
esac
