from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO


class DaemonAlreadyRunning(RuntimeError):
    """Raised when another SLIMHUB daemon owns the runtime lock."""


class DaemonInstanceLock:
    """Process-lifetime advisory lock preventing concurrent daemon instances."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: TextIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise DaemonAlreadyRunning(
                "SLIMHUB daemon is already running; start command ignored"
            ) from exc
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{os.getpid()}\n")
        lock_file.flush()
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def daemon_lock_is_held(path: Path) -> bool:
    """Check lock ownership without treating a stale lock file as running."""
    probe = DaemonInstanceLock(path)
    try:
        probe.acquire()
    except DaemonAlreadyRunning:
        return True
    probe.release()
    return False
