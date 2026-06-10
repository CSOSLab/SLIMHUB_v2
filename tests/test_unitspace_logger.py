from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.events import CommandEvent
from slimhub.logging import UnitspaceMovementLogger


class UnitspaceMovementLoggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_movement_logger_writes_exit_to_enter_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = UnitspaceMovementLogger(AppPaths.from_base(tmpdir))

            await logger.log(
                0.0,
                [
                    CommandEvent("AA:BB:CC:DD:EE:02", "enter", "TOILET"),
                    CommandEvent("AA:BB:CC:DD:EE:01", "exit", "LIVING"),
                ],
            )

            date = datetime.fromtimestamp(0.0).strftime("%Y-%m-%d")
            path = Path(tmpdir) / "logs" / "unitspace" / f"{date}.log"
            lines = path.read_text(encoding="utf-8").splitlines()
            timestamp = datetime.fromtimestamp(0.0).strftime("%Y-%m-%d %H:%M:%S")
            self.assertEqual(
                lines,
                [f"{timestamp}    INFO ~~~ LIVING : EXIT >>>> TOILET : ENTER"],
            )

    async def test_movement_logger_writes_single_enter_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = UnitspaceMovementLogger(AppPaths.from_base(tmpdir))

            await logger.log(
                0.0,
                [CommandEvent("AA:BB:CC:DD:EE:01", "enter", "ENTRY")],
            )

            date = datetime.fromtimestamp(0.0).strftime("%Y-%m-%d")
            path = Path(tmpdir) / "logs" / "unitspace" / f"{date}.log"
            timestamp = datetime.fromtimestamp(0.0).strftime("%Y-%m-%d %H:%M:%S")
            self.assertEqual(
                path.read_text(encoding="utf-8").strip(),
                f"{timestamp}    INFO ~~~ ENTRY : ENTER",
            )


if __name__ == "__main__":
    unittest.main()
