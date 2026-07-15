from __future__ import annotations

import tempfile
import unittest
from datetime import datetime

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

    def test_ensure_removes_old_feature_noise_but_preserves_operator_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            paths.programdata_dir.mkdir(parents=True)
            paths.display_dir.mkdir(parents=True)
            content = (
                "2026-07-15 09:00:00  BEDROOM [EVENT] - Sound 'speech_tv' was detected\n"
                "2026-07-15 09:01:00  BEDROOM [EVENT] - EXIT value: 20\n"
                "2026-07-15 09:01:00  BEDROOM [INFERENCE] COMPLETE: watchTV, "
                "sequence: D0_S2_D1_, truth: 0.87, missing: \n"
            )
            paths.display_path.write_text(content, encoding="utf-8")
            daily = paths.display_dir / f"{datetime.now().strftime('%Y-%m-%d')}.txt"
            daily.write_text(content, encoding="utf-8")

            DisplayWriter(paths).ensure()

            for path in (paths.display_path, daily):
                current = path.read_text(encoding="utf-8")
                self.assertNotIn("Sound", current)
                self.assertIn("EXIT value: 20", current)
                self.assertIn("[INFERENCE] COMPLETE: watchTV", current)

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
            self.assertNotIn("temperature", current)
            debug_path = (
                paths.data_dir
                / "ENTRY"
                / "DEAN_NODE_V2"
                / event.mac
                / "inference"
                / "debugstr"
                / "1970-01-01.txt"
            )
            debug = debug_path.read_text(encoding="utf-8")
            self.assertIn('"type": "DEBUG"', debug)
            self.assertIn('"event": "ENTER"', debug)

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
                "truth: 0.87, missing: ",
                current,
            )
            self.assertNotIn("missing: 0", current)
            debug_path = (
                paths.data_dir
                / "TOILET"
                / "DEAN_NODE_V2"
                / "AA:BB:CC:DD:EE:FF"
                / "inference"
                / "debugstr"
                / "1970-01-01.txt"
            )
            debug = debug_path.read_text(encoding="utf-8")
            self.assertIn('"type": "INFERENCE"', debug)
            self.assertIn('"ADL": "handwash"', debug)

    def test_suppresses_predetect_inference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            writer = DisplayWriter(paths)
            writer.ensure()

            writer.write_multimodal(
                MultimodalRecord(
                    kind="adl_result",
                    mac="AA:BB:CC:DD:EE:FF",
                    timestamp=0.0,
                    data={
                        "location": "TOILET",
                        "event": "PREDETECT",
                        "adl": "handwash",
                    },
                )
            )

            self.assertEqual(paths.display_path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
