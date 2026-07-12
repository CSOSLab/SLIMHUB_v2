from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.events import AlertEvent, ConnectionStateEvent, RawDataEvent, ReportEvent
from slimhub.logging import RawDataLogger
from slimhub.protocol.nus import AlertPacket, RawDataPacket, ReportPacket


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
                / "DEAN_NODE_V2"
                / "AA:BB:CC:DD:EE:FF"
                / "inference"
                / "rawdata"
                / "1970-01-01.txt"
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("time,GridEye,Direction"))
            self.assertIn(",1,1,0,0.00,0,0,0,0,0,", lines[1])

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
            logger = RawDataLogger(paths)
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
            logger = RawDataLogger(paths)
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


if __name__ == "__main__":
    unittest.main()
