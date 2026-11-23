"""Progress for service startup and transfer streams on terminals and in logs."""

from contextlib import contextmanager
from shutil import get_terminal_size
import sys
from threading import Event, Thread
from time import monotonic
from typing import Iterator


@contextmanager
def wait_status(message: str) -> Iterator[None]:
    """Report elapsed time during a service readiness wait."""
    finished = Event()
    started = monotonic()

    def report() -> None:
        while not finished.wait(10):
            print(f"{message} ({monotonic() - started:.0f}s elapsed).", file=sys.stderr, flush=True)

    reporter = Thread(target=report, daemon=True)
    reporter.start()
    try:
        yield
    finally:
        finished.set()
        reporter.join()


def format_bytes(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            digits = 0 if unit == "B" else 1
            return f"{size:.{digits}f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


class Progress:
    def __init__(self, label: str, total: int | None = None, *, docker: bool = False) -> None:
        self.label = label
        self.total = total
        self.docker = docker
        self.bytes = 0
        self.stream = sys.stderr
        self.columns = get_terminal_size().columns if self.stream.isatty() else 0
        self.terminal = self.columns >= 72
        self.interval = 0.2 if self.terminal else 5.0
        self.width = 0

    def __enter__(self) -> "Progress":
        self.started = monotonic()
        self.last_update = self.started
        self.last_bytes = 0
        self.render(self.started)
        if self.docker:
            self.finished = Event()
            self.reporter = Thread(target=self.refresh, daemon=True)
            self.reporter.start()
        return self

    def __exit__(self, error, *_):
        if self.docker:
            self.finished.set()
            self.reporter.join()
        self.render(monotonic(), "failed" if error else "done")

    def refresh(self) -> None:
        # Docker can prepare layers or finalize a load while its pipe is idle.
        while not self.finished.wait(self.interval):
            self.render(monotonic())

    def advance(self, count: int) -> None:
        self.bytes += count
        if self.docker:
            return
        now = monotonic()
        if now - self.last_update >= self.interval:
            self.render(now)

    def render(self, now: float, status: str = "") -> None:
        transferred = self.bytes
        elapsed = now - self.started
        if self.docker and not status:
            duration = now - self.last_update
            rate = (transferred - self.last_bytes) / duration if duration else 0
        else:
            rate = transferred / elapsed if elapsed else 0
        self.last_update = now
        self.last_bytes = transferred
        timing = f"{elapsed:.1f}s"
        amount = format_bytes(transferred)
        if self.total is not None:
            remaining = max(0, self.total - transferred)
            # Floor the percentage so it reaches 100 only after all bytes move.
            percent = 100 * min(transferred, self.total) // self.total if self.total else 100
            amount = f"{percent:3}% {amount}/{format_bytes(self.total)}"
            if remaining and rate and not status:
                timing = f"ETA {remaining / rate:.0f}s"
        label = f"{self.label}: "
        line = f"{amount}  {format_bytes(rate)}/s  {timing}"
        if status:
            line += f"  {status}"
        elif self.docker and not rate:
            line += "  waiting for Docker"
        if self.terminal:
            slots = min(10, self.columns - len(label) - len(line) - 4)
            if self.total is not None and slots > 0:
                filled = percent * slots // 100
                label += f"[{'#' * filled}{'-' * (slots - filled)}] "
        text = label + line
        if self.terminal:
            self.stream.write("\r" + text.ljust(self.width) + ("\n" if status else ""))
            self.width = len(text)
        else:
            self.stream.write(text + "\n")
        self.stream.flush()


class TrackedStream:
    """Count bytes as tarfile reads or writes a wrapped stream."""

    def __init__(self, stream, progress: Progress) -> None:
        self.stream = stream
        self.progress = progress

    def read(self, size: int) -> bytes:
        data = self.stream.read(size)
        self.progress.advance(len(data))
        return data

    def write(self, data: bytes) -> int:
        written = self.stream.write(data)
        self.progress.advance(written)
        return written
