from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.events import AlertEvent, ConnectionStateEvent, RawDataEvent, ReportEvent
from slimhub.logging import RawDataLogger
from slimhub.logging.raw_logger import CANONICAL_SOUND_LABELS, CSV_FIELDS
from slimhub.protocol.nus import AlertPacket, RawDataPacket, ReportPacket


class RawLoggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_logger_quarantines_undefined_location(self) -> None:
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

            self.assertFalse(list(paths.data_dir.rglob("rawdata/*.txt")))
            self.assertEqual(logger.health["quarantined"], 2)
            diagnostic = next(
                (paths.programdata_dir / "reports").glob("*.jsonl")
            ).read_text(encoding="utf-8")
            self.assertIn("raw_location_quarantined", diagnostic)

    async def test_b_tflm_sound_schema_has_ten_labels_and_never_dequantizes_padding_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            logger = RawDataLogger(paths)
            packet = RawDataPacket(
                flag_human_presence=0,
                detected=0,
                flag_env=0,
                temperature_c=0.0,
                humidity=0,
                iaq=0,
                eco2=0,
                bvoc=0,
                accuracy=0,
                flag_sound=1,
                sound=[-128, -64, 0, 64, 127, 0, 0, 0, 12, 34, 99, 99, 99, 99, 99, 99],
                is_pir_human_detection_event=False,
            )
            event = RawDataEvent(
                timestamp=0.0,
                mac="AA:BB:CC:DD:EE:FF",
                location="ENTRY",
                packet=packet,
                payload=b"raw",
                sound_schema_version="b-tflm-v1",
                sound_class_count=10,
            )

            await logger.write_event(event)

            path = next((Path(tmpdir) / "data").glob("*/*/*/inference/rawdata/*.txt"))
            header, row = path.read_text(encoding="utf-8").splitlines()
            self.assertIn("watering_low", header)
            self.assertIn("watering_high", header)
            self.assertIn("microwave", header)
            self.assertIn("cooking", header)
            self.assertEqual(len(header.split(",")), 24)
            # An int8 zero is valid inside a declared 10-class tensor.
            self.assertIn(",0.5,", f",{row},")

    async def test_raw_schema2_toilet_slots_use_canonical_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            packet = RawDataPacket(
                flag_human_presence=0,
                detected=0,
                flag_env=0,
                temperature_c=0.0,
                humidity=0,
                iaq=0,
                eco2=0,
                bvoc=0,
                accuracy=0,
                flag_sound=1,
                sound=[-128, -128, -128, -128, 64, -128, 96, -128, -128, -128, -128, -128, -128, -128, 127, 127],
                is_pir_human_detection_event=False,
            )
            event = RawDataEvent(
                timestamp=0.0,
                mac="AA:BB:CC:DD:EE:01",
                location="TOILET",
                packet=packet,
                payload=b"raw",
                sound_raw_schema=2,
            )

            await RawDataLogger(AppPaths.from_base(tmpdir)).write_event(event)

            path = next((Path(tmpdir) / "data").glob("*/*/*/inference/rawdata/1970-01-01.txt"))
            with path.open(encoding="utf-8", newline="") as f:
                row = next(csv.DictReader(f))
            self.assertEqual(float(row["brushing"]), (64 + 128) / 256)
            self.assertEqual(float(row["flushing"]), (96 + 128) / 256)
            self.assertEqual(float(row["cooking"]), 0.0)
            self.assertEqual(float(row["snoring"]), 0.0)

    async def test_raw_schema2_kitchen_tensor_results_are_in_slots_10_and_11(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scores = [-128] * 16
            scores[10] = 32
            scores[11] = 96
            event = RawDataEvent(
                timestamp=0.0,
                mac="AA:BB:CC:DD:EE:02",
                location="KITCHEN",
                packet=RawDataPacket(
                    flag_human_presence=0,
                    detected=0,
                    flag_env=0,
                    temperature_c=0.0,
                    humidity=0,
                    iaq=0,
                    eco2=0,
                    bvoc=0,
                    accuracy=0,
                    flag_sound=1,
                    sound=scores,
                    is_pir_human_detection_event=False,
                ),
                payload=b"raw",
                sound_raw_schema=2,
            )

            await RawDataLogger(AppPaths.from_base(tmpdir)).write_event(event)

            path = next((Path(tmpdir) / "data").glob("*/*/*/inference/rawdata/1970-01-01.txt"))
            with path.open(encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                row = next(reader)
            self.assertEqual(float(row["cooking"]), (32 + 128) / 256)
            self.assertEqual(float(row["microwave"]), (96 + 128) / 256)
            self.assertNotIn("reserved", ",".join(reader.fieldnames or []))
            self.assertEqual(len(reader.fieldnames or []), len(CSV_FIELDS))
            self.assertEqual(set(CANONICAL_SOUND_LABELS), set(CSV_FIELDS[10:]))

    async def test_all_locations_share_canonical_header_and_row_width(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = RawDataLogger(AppPaths.from_base(tmpdir))
            for index, location in enumerate(
                ("TOILET", "KITCHEN", "LIVING", "BEDROOM"),
                start=1,
            ):
                event = RawDataEvent(
                    timestamp=0.0,
                    mac=f"AA:BB:CC:DD:EE:{index:02X}",
                    location=location,
                    packet=RawDataPacket(
                        flag_human_presence=0,
                        detected=0,
                        flag_env=0,
                        temperature_c=0.0,
                        humidity=0,
                        iaq=0,
                        eco2=0,
                        bvoc=0,
                        accuracy=0,
                        flag_sound=1,
                        sound=[-128] * 16,
                        is_pir_human_detection_event=False,
                    ),
                    payload=b"raw",
                    sound_raw_schema=2,
                )
                await logger.write_event(event)

            paths = sorted(
                (Path(tmpdir) / "data").glob(
                    "*/*/*/inference/rawdata/1970-01-01.txt"
                )
            )
            self.assertEqual(len(paths), 4)
            rows = [
                list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
                for path in paths
            ]
            for lines in rows:
                self.assertEqual(lines[0], CSV_FIELDS)
                self.assertEqual(len(lines[1]), len(CSV_FIELDS))
                self.assertTrue(
                    all(float(value) == 0.0 for value in lines[1][10:])
                )

    async def test_incompatible_flush_end_header_is_rotated_before_append(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            target = (
                paths.data_dir
                / "TOILET"
                / "DEAN_NODE_V2"
                / "AA:BB:CC:DD:EE:01"
                / "inference"
                / "rawdata"
                / "1970-01-01.txt"
            )
            target.parent.mkdir(parents=True)
            legacy_header = [*CSV_FIELDS]
            legacy_header[legacy_header.index("flushing_end")] = "flush_end"
            target.write_text(
                ",".join(legacy_header) + "\nlegacy,row\n",
                encoding="utf-8",
            )
            event = RawDataEvent(
                timestamp=0.0,
                mac="AA:BB:CC:DD:EE:01",
                location="TOILET",
                packet=RawDataPacket(
                    flag_human_presence=0,
                    detected=0,
                    flag_env=0,
                    temperature_c=0.0,
                    humidity=0,
                    iaq=0,
                    eco2=0,
                    bvoc=0,
                    accuracy=0,
                    flag_sound=1,
                    sound=[-128] * 16,
                    is_pir_human_detection_event=False,
                ),
                payload=b"raw",
                sound_raw_schema=2,
            )

            await RawDataLogger(paths).write_event(event)

            rotated = list(target.parent.glob("1970-01-01.legacy-*.txt"))
            self.assertEqual(len(rotated), 1)
            self.assertIn("legacy,row", rotated[0].read_text(encoding="utf-8"))
            self.assertEqual(
                target.read_text(encoding="utf-8").splitlines()[0],
                ",".join(CSV_FIELDS),
            )

    async def test_profile_tensor_writes_union_without_raw_schema2(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            event = RawDataEvent(
                timestamp=0.0,
                mac="AA:BB:CC:DD:EE:01",
                location="TOILET",
                packet=RawDataPacket(
                    flag_human_presence=0,
                    detected=0,
                    flag_env=0,
                    temperature_c=0.0,
                    humidity=0,
                    iaq=0,
                    eco2=0,
                    bvoc=0,
                    accuracy=0,
                    flag_sound=1,
                    sound=[0] * 16,
                    is_pir_human_detection_event=False,
                ),
                payload=b"raw",
            )

            await RawDataLogger(paths).write_event(event)

            raw_dir = next(paths.data_dir.glob("*/*/*/inference/rawdata"))
            self.assertTrue((raw_dir / "1970-01-01.txt").exists())
            self.assertFalse(
                (raw_dir / "1970-01-01.legacy-raw-schema.txt").exists()
            )

    async def test_alert_logger_writes_data_directory_without_rawdata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            logger = RawDataLogger(paths)
            event = AlertEvent(
                timestamp=0.0,
                mac="AA:BB:CC:DD:EE:FF",
                location="ENTRY",
                packet=AlertPacket("ready"),
                payload=b"ready",
            )

            await logger.write_alert(event)

            path = (
                Path(tmpdir)
                / "data"
                / "ENTRY"
                / "DEAN_NODE_V2"
                / "AA:BB:CC:DD:EE:FF"
                / "inference"
                / "debugstr"
                / "1970-01-01.txt"
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[0].endswith(",ready"))

    async def test_report_logger_writes_structured_jsonl_with_connection_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            logger = RawDataLogger(paths, audit_mode="full")
            report = ReportPacket(
                message="src=INOUT,event=ENTER,signal=enter,code=10",
                fields={
                    "src": "INOUT",
                    "event": "ENTER",
                    "signal": "enter",
                    "code": "10",
                },
            )

            await logger.write_connection_state(
                ConnectionStateEvent(
                    timestamp=0.0,
                    address="AA:BB:CC:DD:EE:00",
                    connected=True,
                )
            )
            await logger.write_report(
                ReportEvent(
                    timestamp=1.0,
                    mac="AA:BB:CC:DD:EE:FF",
                    source_address="AA:BB:CC:DD:EE:00",
                    location="ENTRY",
                    packet=report,
                    payload=report.message.encode("utf-8"),
                    connected=True,
                )
            )

            path = Path(tmpdir) / "programdata" / "reports" / "1970-01-01.jsonl"
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows[0]["kind"], "connection")
            self.assertTrue(rows[0]["connected"])
            self.assertEqual(rows[1]["kind"], "report")
            self.assertEqual(rows[1]["mac"], "AA:BB:CC:DD:EE:FF")
            self.assertEqual(rows[1]["ble_address"], "AA:BB:CC:DD:EE:00")
            self.assertEqual(rows[1]["src"], "INOUT")
            self.assertEqual(rows[1]["event"], "ENTER")
            self.assertEqual(rows[1]["fields"]["code"], "10")
            self.assertTrue(rows[1]["connected"])

    async def test_usd_status_report_promotes_battery_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            logger = RawDataLogger(paths, audit_mode="full")
            report = ReportPacket(
                message=(
                    "src=USD,event=STATUS,uptime=12345,file=LOG/001.CSV,ok=1,"
                    "batt_v=3.980,batt_mv=3980,batt_pct=75,batt_rem_mah=1125,"
                    "usb=0,chg=1,sd=0"
                ),
                fields={
                    "src": "USD",
                    "event": "STATUS",
                    "uptime": "12345",
                    "file": "LOG/001.CSV",
                    "ok": "1",
                    "batt_v": "3.980",
                    "batt_mv": "3980",
                    "batt_pct": "75",
                    "batt_rem_mah": "1125",
                    "usb": "0",
                    "chg": "1",
                    "sd": "0",
                },
            )

            await logger.write_report(
                ReportEvent(
                    timestamp=0.0,
                    mac="AA:BB:CC:DD:EE:FF",
                    source_address="AA:BB:CC:DD:EE:00",
                    location="ENTRY",
                    packet=report,
                    payload=report.message.encode("utf-8"),
                    connected=True,
                )
            )

            path = Path(tmpdir) / "programdata" / "reports" / "1970-01-01.jsonl"
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(row["src"], "USD")
            self.assertEqual(row["event"], "STATUS")
            self.assertEqual(row["batt_mv"], "3980")
            self.assertEqual(row["batt_v"], "3.980")
            self.assertEqual(row["batt_pct"], "75")
            self.assertEqual(row["batt_rem_mah"], "1125")
            self.assertEqual(row["usb"], "0")
            self.assertEqual(row["chg"], "1")
            self.assertEqual(row["sd"], "0")
            self.assertEqual(row["file"], "LOG/001.CSV")
            self.assertEqual(row["uptime"], "12345")
            self.assertEqual(row["ok"], "1")

    async def test_minimal_audit_does_not_duplicate_routine_raw_or_report_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            logger = RawDataLogger(paths, audit_mode="minimal")
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
            await logger.write_event(
                RawDataEvent(
                    timestamp=0.0,
                    mac="AA:BB:CC:DD:EE:FF",
                    location="ENTRY",
                    packet=packet,
                    payload=b"raw",
                )
            )
            await logger.write_report(
                ReportEvent(
                    timestamp=0.0,
                    mac="AA:BB:CC:DD:EE:FF",
                    source_address="AA:BB:CC:DD:EE:FF",
                    location="ENTRY",
                    packet=ReportPacket("src=USD,event=STATUS", {"src": "USD"}),
                    payload=b"report",
                )
            )

            self.assertTrue(next(paths.data_dir.glob("*/*/*/inference/rawdata/*.txt")))
            self.assertFalse((paths.programdata_dir / "reports").exists())


if __name__ == "__main__":
    unittest.main()
