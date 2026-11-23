# The lock stays beside loaf/ so cleanup can remove the whole cache.
lock_loaf_runtime() {
    case ${XDG_DATA_HOME:-} in
        /*) loaf_data_home=$XDG_DATA_HOME ;;
        *) loaf_data_home="$HOME/.local/share" ;;
    esac
    mkdir -p -- "$loaf_data_home"
    local lock="$loaf_data_home/.loaf-runtime.lock"
    local cache="$loaf_data_home/loaf"
    if [[ -L "$lock" || ( -e "$lock" && ( ! -f "$lock" || ! -O "$lock" ) ) ||
          -L "$cache" || ( -e "$cache" && ( ! -d "$cache" || ! -O "$cache" ) ) ]]; then
        echo "The loaf runtime cache and lock must be local paths owned by this user." >&2
        return 1
    fi
    exec {LOAF_RUNTIME_LOCK_FD}>>"$lock"
    flock --shared "$LOAF_RUNTIME_LOCK_FD"
    export LOAF_RUNTIME_LOCK_FD
}
