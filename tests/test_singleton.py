from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from slimhub.singleton import (
    DaemonAlreadyRunning,
    DaemonInstanceLock,
    daemon_lock_is_held,
)


class DaemonInstanceLockTests(unittest.TestCase):
    def test_only_one_lock_owner_and_stale_file_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "slimhub.lock"
            first = DaemonInstanceLock(path)
            second = DaemonInstanceLock(path)

            first.acquire()
            self.assertTrue(daemon_lock_is_held(path))
            with self.assertRaises(DaemonAlreadyRunning):
                second.acquire()
            first.release()

            self.assertFalse(daemon_lock_is_held(path))
            second.acquire()
            second.release()

    def test_lock_file_contains_current_owner_pid_while_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "slimhub.lock"
            lock = DaemonInstanceLock(path)

            lock.acquire()

            self.assertTrue(path.read_text(encoding="utf-8").strip().isdigit())
            lock.release()


if __name__ == "__main__":
    unittest.main()
