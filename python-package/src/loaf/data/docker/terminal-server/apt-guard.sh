#!/bin/sh
# Installed as apt and apt-get ahead of /usr/bin on PATH in terminal runners.
# Runners have a read-only root filesystem, so package changes go through the terminal server.
command=$(basename "$0")
case $command in apt|apt-get) ;; *) command=apt-get ;; esac
# The package installer runs as root and uses the real tools.
if [ "$(id -u)" = 0 ]; then
    exec "/usr/bin/$command" "$@"
fi
for word in "$@"; do
    case $word in
        -*) continue ;;
        install|reinstall|remove|purge|autoremove|autopurge|update|upgrade|full-upgrade|dist-upgrade|\
satisfy|build-dep|clean|autoclean|edit-sources|markauto|unmarkauto)
            cat >&2 <<EOF
$command: packages cannot be changed from this shell.
The assistant changes the APT packages of this chat with a plain command, for example
  apt-get install PACKAGE    or    apt-get remove PACKAGE
on its own, without pipes or other commands. The chat's terminal then restarts with
the changed packages and keeps the files in /home/user and /home/chat.
Upgrades come with the shared terminal image.
EOF
            exit 100 ;;
        *) break ;;
    esac
done
exec "/usr/bin/$command" "$@"
