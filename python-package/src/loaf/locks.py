"""Coordinate project changes and shared runtime cleanup."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import Iterator


PROJECT_LOCK = ".run.lock"
RUNTIME_LOCK = ".loaf-runtime.lock"


class AlreadyRunningError(RuntimeError):
    """Another process holds a lock needed by the command."""


def data_home() -> Path:
    configured = Path(os.environ.get("XDG_DATA_HOME") or ".")
    return configured if configured.is_absolute() else Path.home() / ".local/share"


@contextmanager
def lock_file(path: Path, message: str) -> Iterator[None]:
    # Do not follow symlinks or open special files when taking a lock.
    if path.is_symlink():
        raise OSError(f"Lock file must be a regular file: {path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"Lock file must be a regular file: {path}")
        if os.name == "nt":
            import msvcrt

            if info.st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise AlreadyRunningError(message) from exc
        else:
            exclusive_lock(descriptor, message)
        yield
    finally:
        os.close(descriptor)
    # Closing releases ownership. Keep the file so waiters share its inode.


def project_lock(path: Path):
    return lock_file(path, "Another launcher is running for this project. Wait for it to finish.")


def exclusive_lock(descriptor: int, message: str) -> None:
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise AlreadyRunningError(message) from exc


@contextmanager
def runtime_cleanup_lock(home: Path) -> Iterator[None]:
    path = home / RUNTIME_LOCK
    message = "Another loaf command is using the runtime. Wait for it to finish."
    inherited = os.environ.get("LOAF_RUNTIME_LOCK_FD", "")
    if inherited.isdecimal():
        descriptor = int(inherited)
        try:
            info = path.lstat()
            same_file = (
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and os.path.samestat(info, os.fstat(descriptor))
            )
        except OSError:
            same_file = False
        if same_file:
            # The standalone launcher already holds a shared lock on this descriptor.
            exclusive_lock(descriptor, message)
            yield
            return
    with lock_file(path, message):
        yield
