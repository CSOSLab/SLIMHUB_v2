from __future__ import annotations

import tempfile
import unittest

from slimhub.config import AppPaths
from slimhub.events import ReportEvent
from slimhub.logging import DisplayWriter
from slimhub.multimodal import MultimodalRecord
from slimhub.protocol.nus import ReportPacket


class DisplayWriterTests(unittest.TestCase):
    def test_ensure_creates_current_display_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)

            DisplayWriter(paths).ensure()

            self.assertTrue(paths.display_path.is_file())
            self.assertTrue(paths.display_dir.is_dir())

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
            self.assertIn("ENTRY [EVENT] - ENTER value: 10", current)
            self.assertIn("KITCHEN [EVENT] - 'temperature' event was detected", current)

    def test_suppresses_diagnostic_inout_and_background_sound(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            writer = DisplayWriter(paths)
            writer.ensure()
            writer.write_inout(
                ReportEvent(
                    timestamp=0.0,
                    mac="AA:BB:CC:DD:EE:FF",
                    source_address="AA:BB:CC:DD:EE:FF",
                    location="ENTRY",
                    payload=b"",
                    packet=ReportPacket(
                        message="",
                        fields={
                            "src": "INOUT",
                            "event": "SEQUENCE",
                            "result": "RADAR_TIMEOUT",
                            "event_id": "NONE",
                        },
                    ),
                )
            )
            writer.write_multimodal(
                MultimodalRecord(
                    kind="feature",
                    mac="AA:BB:CC:DD:EE:FF",
                    timestamp=0.0,
                    data={"location": "ENTRY", "event": "SOUND", "label": "BACKGROUND"},
                )
            )

            self.assertEqual(paths.display_path.read_text(encoding="utf-8"), "")

    def test_formats_adl_like_the_deployed_operator_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            writer = DisplayWriter(paths)

            writer.write_multimodal(
                MultimodalRecord(
                    kind="adl_result",
                    mac="AA:BB:CC:DD:EE:FF",
                    timestamp=0.0,
                    data={
                        "location": "TOILET",
                        "event": "COMPLETE",
                        "adl": "handwash",
                        "sequence": "D0_S9_D1_",
                        "truth": 87,
                        "missing": 0,
                    },
                )
            )

            current = paths.display_path.read_text(encoding="utf-8")
            self.assertIn(
                "TOILET [INFERENCE] COMPLETE: handwash, sequence: D0_S9_D1_, "
                "truth: 0.87, missing: 0",
                current,
            )


if __name__ == "__main__":
    unittest.main()
