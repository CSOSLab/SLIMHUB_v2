from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.events import RawDataEvent
from slimhub.logging import RawDataLogger
from slimhub.protocol.nus import RawDataPacket


class RawLoggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_logger_appends_header_once_with_undefined_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            logger = RawDataLogger(paths)
            packet = RawDataPacket(
                flag_human_presence=1,
                detected=1,
                flag_env=0,
                temperature_c=0.0,
                humidity=0,
                iaq=0,
                eco2=0,
                bvoc=0,
                accuracy=0,
                flag_sound=0,
                sound=[0] * 16,
                is_pir_human_detection_event=True,
            )
            event = RawDataEvent(
                timestamp=0.0,
                mac="AA:BB:CC:DD:EE:FF",
                location="",
                packet=packet,
                payload=b"",
            )

            await logger.write_event(event)
            await logger.write_event(event)

            path = (
                Path(tmpdir)
                / "data"
                / "undefined"
                / "AA:BB:CC:DD:EE:FF"
                / "rawdata"
                / "1970-01-01.csv"
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertIn("timestamp,mac,location", lines[0])
            self.assertEqual(lines[1].count("AA:BB:CC:DD:EE:FF"), 1)


if __name__ == "__main__":
    unittest.main()
