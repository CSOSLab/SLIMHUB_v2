from __future__ import annotations

import tempfile
import unittest

from slimhub.config import AppPaths
from slimhub.events import ReportEvent
from slimhub.logging import DisplayWriter
from slimhub.multimodal import MultimodalRecord
from slimhub.protocol.nus import ReportPacket


class DisplayWriterTests(unittest.TestCase):
    def test_writes_current_and_daily_display_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            writer = DisplayWriter(paths)
            event = ReportEvent(
                timestamp=0.0,
                receipt_timestamp=0.0,
                mac="AA:BB:CC:DD:EE:FF",
                source_address="AA:BB:CC:DD:EE:FF",
                location="ENTRY",
                payload=b"",
                packet=ReportPacket(
                    message="",
                    fields={
                        "src": "INOUT",
                        "event": "SEQUENCE",
                        "result": "ENTER_CONFIRMED",
                        "event_id": "D0",
                    },
                ),
            )

            writer.write_inout(event)
            writer.write_multimodal(
                MultimodalRecord(
                    kind="feature",
                    mac=event.mac,
                    timestamp=0.0,
                    data={
                        "location": "KITCHEN",
                        "event": "ENV",
                        "event_id": "E0",
                        "canonical_name": "temperature",
                        "confidence": 91,
                    },
                )
            )

            current = paths.display_path.read_text(encoding="utf-8")
            daily = (paths.display_dir / "1970-01-01.txt").read_text(encoding="utf-8")
            self.assertEqual(current, daily)
            self.assertIn("ENTRY [INOUT] ENTER_CONFIRMED D0", current)
            self.assertIn("KITCHEN [EVENT] ENV E0 (temperature) confidence=91", current)


if __name__ == "__main__":
    unittest.main()
