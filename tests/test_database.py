from __future__ import annotations

import json
import struct
import tempfile
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from slimhub.config import AppPaths
from slimhub.integrations.battery_log import (
    SEOUL,
    battery_export_state_path,
    export_battery_logs,
    percent_from_mv,
)
from slimhub.integrations.data_ingest import DataDirectoryReader
from slimhub.integrations.database import DataDirectoryDatabaseUpdater


MAC = "AA:BB:CC:DD:EE:FF"


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, _: object = None) -> None:
        if "created_time BETWEEN" in query:
            table = next(
                name for name in ("event_adl", "in_out", "battery_status")
                if f"`{name}`" in query
            )
            self.rows = list(self.connection.existing.get(table, []))
        elif query.startswith("SHOW COLUMNS"):
            self.rows = [("id",), ("house_mac",)]
        elif "`event_adl`" in query:
            self.rows = [(5, "house-a")]
        elif "`in_out`" in query:
            self.rows = [(7, "house-a")]

    def executemany(self, query: str, rows: object) -> None:
        self.connection.inserts.append((query, list(rows)))
        if self.connection.fail_table and f"`{self.connection.fail_table}`" in query:
            raise RuntimeError("simulated remote failure")

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class FakeConnection:
    def __init__(
        self,
        *,
        fail_table: str | None = None,
        existing: dict[str, list[tuple[object, ...]]] | None = None,
    ) -> None:
        self.fail_table = fail_table
        self.existing = existing or {}
        self.inserts: list[tuple[str, object]] = []
        self.commits = 0

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


RAW_HEADER = (
    "time,GridEye,Direction,ENV,temp,humid,iaq,eco2,bvoc,SOUND,background,hitting,"
    "speech_tv,air_appliances,brushing,peeing,flushing,flushing_end,watering_low,"
    "watering_high,cooking,microwave,appliances,snoring,gas_oven,reserved\n"
)


def raw_row(time: str, grideye: int, direction: int) -> str:
    return f"{time},{grideye},{direction}" + ",0" * 23 + "\n"


def event_line(time: str, event: str, value: int) -> str:
    return json.dumps(
        {"device": MAC, "type": "EVENT", "event": event, "value": value, "timestamp": time}
    ) + "\n"


def battery_file_line(time: str, percentage: int | None, batt_mv: int = 3991) -> str:
    return json.dumps(
        {"device": MAC, "type": "BATTERY", "percentage": percentage, "batt_mv": batt_mv,
         "usb": 0, "uptime_s": 100, "timestamp": time}
    ) + "\n"


def dbat_record(stream: int, seq: int, vbat_mv: int, uptime_ms: int, *, usb_pin: int = 1) -> str:
    payload = struct.pack(
        "<BBbbIhhhhiiQIiiI", 1, 12, usb_pin, 0, 0x20, 0, 0, 0, -1,
        vbat_mv * 3 // 4, vbat_mv, uptime_ms, 0, 0, 0, 0,
    )
    head = b"DBAT" + bytes((1, 1)) + struct.pack("<HQQ", 48, stream, seq)
    body = head + payload
    return (body + struct.pack("<I", zlib.crc32(body))).hex()


def vlb1_row(mac: str, stream: int, receipt_ts: float, records: list[str]) -> str:
    return json.dumps(
        {"format": "VLB1", "mac": mac, "receipt_ts": receipt_ts, "stream": f"{stream:016x}",
         "decoded": {"records": [{"data": data, "seq": i + 1} for i, data in enumerate(records)]}}
    ) + "\n"


def data_path(
    paths: AppPaths,
    stream: str,
    *,
    location: str = "ENTRY",
    date: str | None = None,
) -> Path:
    date = date or datetime.now().strftime("%Y-%m-%d")
    path = (
        paths.data_dir
        / location
        / "DEAN_NODE_V2"
        / MAC
        / "inference"
        / stream
        / f"{date}.txt"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class DataDirectoryDatabaseUpdaterTests(unittest.TestCase):
    def test_accepts_deployed_legacy_database_environment_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            updater = DataDirectoryDatabaseUpdater(
                AppPaths.from_base(tmpdir),
                environ={
                    "LOCAL_DB_HOST": "localhost",
                    "LOCAL_DB_PORT": "3307",
                    "LOCAL_DB_USER": "local-user",
                    "LOCAL_DB_PASS": "local-pass",
                    "LOCAL_DB_NAME": "adl_event",
                    "REMOTE_DB_HOST": "remote.example",
                    "REMOTE_DB_PORT": "4404",
                    "REMOTE_DB_USER": "remote-user",
                    "REMOTE_DB_PASS": "remote-pass",
                    "REMOTE_DB_NAME": "adl_raw",
                    "ADL_TABLE": "legacy_adl",
                },
            )

            local = updater._settings("LOCAL", required=True)
            remote = updater._settings("REMOTE", required=True)

            self.assertEqual(local.port, 3307)
            self.assertEqual(local.database, "adl_event")
            self.assertEqual(remote.host, "remote.example")
            self.assertEqual(remote.port, 4404)
            self.assertEqual(updater.adl_table, "legacy_adl")

    def test_data_reader_maps_event_inout_and_every_inference_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            # rawdata is not a DB source (the hub's EVENT lines in debugstr are).
            data_path(paths, "rawdata", date="2026-07-15").write_text(
                RAW_HEADER + raw_row("2026-07-15 10:00:01", 1, 10), encoding="utf-8"
            )
            debug = data_path(paths, "debugstr", date="2026-07-15")
            records = [
                {"device": MAC, "type": "INFERENCE", "status": "PRE-DETECT",
                 "ADL": "pee", "truth": 0.5, "timestamp": "2026-07-15 10:00:01"},
                {"device": MAC, "type": "INFERENCE", "status": "POP", "ADL": "pee",
                 "sequence": "D0_S5_", "truth": 88, "timestamp": "2026-07-15 10:00:02"},
                {"device": MAC, "type": "INFERENCE", "status": "NO_MATCH", "ADL": "none",
                 "sequence": "D0_S2_D1_", "truth": 0.0, "timestamp": "2026-07-15 10:00:03"},
                {"device": MAC, "type": "EVENT", "event": "ENTER", "value": 10,
                 "timestamp": "2026-07-15 10:00:04"},
                {"device": MAC, "type": "EVENT", "event": "EXIT", "value": 20,
                 "timestamp": "2026-07-15 10:00:05"},
                # The Node's own DEBUG lines are not in_out rows.
                {"device": MAC, "type": "DEBUG", "event": "EXIT", "value": 20,
                 "timestamp": "2026-07-15 10:00:06"},
                # NO_MATCH with no event between D0 and D1 is not stored ...
                {"device": MAC, "type": "INFERENCE", "status": "NO_MATCH", "ADL": "none",
                 "sequence": "D0_D1_", "sequence_list": "", "truth": 0.0,
                 "timestamp": "2026-07-15 10:00:07"},
                # ... but an ADL with an empty sequence still is.
                {"device": MAC, "type": "INFERENCE", "status": "COMPLETE", "ADL": "sleeping",
                 "sequence": "D0_D1_", "sequence_list": "", "truth": 0.6,
                 "timestamp": "2026-07-15 10:00:08"},
            ]
            debug.write_text(
                "".join(json.dumps(record) + "\n" for record in records)
                + "2026-06-01 18:39:54,This is temporary packet : alert\n",
                encoding="utf-8",
            )
            # Manual copies and non-room folders are not DB sources.
            data_path(paths, "debugstr", location="undefined", date="2026-07-15").write_text(
                json.dumps(records[3]) + "\n", encoding="utf-8"
            )

            batch = DataDirectoryReader(paths).read(house_mac="11:22:33:44:55:66")

            self.assertEqual(
                batch.inout_rows,
                [
                    ("11:22:33:44:55:66", f"ENTRY:{MAC}", "2026-07-15 10:00:04", 10),
                    ("11:22:33:44:55:66", f"ENTRY:{MAC}", "2026-07-15 10:00:05", 20),
                ],
            )
            self.assertEqual(batch.files, 1)
            self.assertEqual(
                batch.adl_rows,
                [
                    ("11:22:33:44:55:66", MAC, "2026-07-15 10:00:01", "", "pee", 0.5),
                    ("11:22:33:44:55:66", MAC, "2026-07-15 10:00:02", "D0_S5_", "pee", 0.88),
                    ("11:22:33:44:55:66", MAC, "2026-07-15 10:00:03", "D0_S2_D1_", "none", 0.0),
                    ("11:22:33:44:55:66", MAC, "2026-07-15 10:00:08", "D0_D1_", "sleeping", 0.6),
                ],
            )

    def test_debugstr_offsets_skip_read_rows_and_realign_inside_a_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            debug = data_path(paths, "debugstr")
            first_line = event_line("2026-10-27 10:00:00", "ENTER", 10)
            debug.write_text(first_line, encoding="utf-8")
            first = DataDirectoryReader(paths).read(house_mac="house")
            key = str(debug.resolve())
            self.assertEqual(len(first.inout_rows), 1)
            self.assertEqual(first.offsets[key], debug.stat().st_size)

            with debug.open("a", encoding="utf-8") as output:
                output.write(event_line("2026-10-27 10:05:00", "EXIT", 20))
            second = DataDirectoryReader(paths, offsets=first.offsets).read(house_mac="house")
            self.assertEqual(
                second.inout_rows, [("house", f"ENTRY:{MAC}", "2026-10-27 10:05:00", 20)]
            )

            # A stale offset inside a line (file rewritten) skips to the next line.
            inside = {key: 5}
            third = DataDirectoryReader(paths, offsets=inside).read(house_mac="house")
            self.assertEqual(
                third.inout_rows, [("house", f"ENTRY:{MAC}", "2026-10-27 10:05:00", 20)]
            )

    def test_data_reader_uses_byte_offsets_and_waits_for_complete_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            debug = data_path(paths, "debugstr")
            complete = json.dumps(
                {
                    "device": MAC,
                    "type": "INFERENCE",
                    "status": "COMPLETE",
                    "ADL": "watchTV",
                    "timestamp": "2026-07-15 10:00:00",
                }
            )
            debug.write_text(complete + "\n" + '{"type":"INFERENCE"', encoding="utf-8")

            first = DataDirectoryReader(paths).read(house_mac="house")
            key = str(debug.resolve())
            self.assertEqual(len(first.adl_rows), 1)
            self.assertLess(first.offsets[key], debug.stat().st_size)

            second = DataDirectoryReader(paths, offsets=first.offsets).read(house_mac="house")
            self.assertEqual(second.adl_rows, [])
            self.assertEqual(second.offsets[key], first.offsets[key])

    def test_ingest_commits_data_rows_then_writes_data_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            raw = data_path(paths, "debugstr")
            raw.write_text(
                event_line("2026-07-15 10:00:00", "EXIT", 20)
                + event_line("2026-07-15 10:00:00", "EXIT", 20)
                + event_line("2026-07-15 10:01:00", "ENTER", 10),
                encoding="utf-8",
            )
            updater = DataDirectoryDatabaseUpdater(
                paths,
                environ={
                    "SLIMHUB_LOCAL_DB_HOST": "localhost",
                    "SLIMHUB_LOCAL_DB_USER": "user",
                    "SLIMHUB_LOCAL_DB_NAME": "adl_event",
                    "SLIMHUB_HOUSE_MAC": "house",
                },
            )
            # The 10:01 row is already in the DB (e.g. committed before a crash).
            local = FakeConnection(
                existing={
                    "in_out": [
                        ("house", f"ENTRY:{MAC}", datetime(2026, 7, 15, 10, 1, 0), 10)
                    ]
                }
            )

            with patch.object(updater, "_connect", return_value=local):
                result = updater.ingest()

            self.assertEqual(result["inout_inserted"], 1)
            self.assertEqual(result["duplicates_skipped"], 2)
            self.assertEqual(
                local.inserts[-1][1],
                [("house", f"ENTRY:{MAC}", "2026-07-15 10:00:00", 20)],
            )
            self.assertEqual(local.commits, 1)
            self.assertIn(
                str(raw.resolve()),
                updater._read_json(paths.db_ingest_offset_path),
            )

    def test_preview_writes_pending_db_rows_without_db_or_offset_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            data_path(paths, "debugstr").write_text(
                event_line("2026-07-15 10:00:00", "EXIT", 20), encoding="utf-8"
            )
            updater = DataDirectoryDatabaseUpdater(
                paths,
                environ={"SLIMHUB_HOUSE_MAC": "house"},
            )
            preview_path = paths.programdata_dir / "preview.txt"

            with patch.object(updater, "_connect") as connect:
                result = updater.write_db_rows_preview(preview_path)

            preview = preview_path.read_text(encoding="utf-8")
            self.assertEqual(result["inout_rows"], 1)
            self.assertIn("[event_adl]", preview)
            self.assertIn("[in_out]", preview)
            self.assertIn(f"house\tENTRY:{MAC}\t2026-07-15 10:00:00\t20", preview)
            self.assertFalse(paths.db_ingest_offset_path.exists())
            connect.assert_not_called()

    def test_battery_reader_maps_percentage_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            data_path(paths, "battery", date="2026-11-02").write_text(
                battery_file_line("2026-11-02 00:01:05", 76)
                # An invalid ADC reading has no percentage and is not a DB row.
                + battery_file_line("2026-11-02 00:06:06", None, batt_mv=-1)
                + event_line("2026-11-02 00:07:00", "ENTER", 10),
                encoding="utf-8",
            )

            batch = DataDirectoryReader(paths).read(house_mac="house")

            self.assertEqual(
                batch.battery_rows, [("house", MAC, "2026-11-02 00:01:05", "76")]
            )
            self.assertEqual(batch.inout_rows, [])
            self.assertEqual(batch.lines, 3)

    def test_ingest_inserts_new_battery_rows_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            data_path(paths, "battery", date="2026-11-02").write_text(
                battery_file_line("2026-11-02 00:01:05", 76)
                + battery_file_line("2026-11-02 00:06:06", 77),
                encoding="utf-8",
            )
            updater = DataDirectoryDatabaseUpdater(
                paths,
                environ={
                    "SLIMHUB_LOCAL_DB_HOST": "localhost",
                    "SLIMHUB_LOCAL_DB_USER": "user",
                    "SLIMHUB_LOCAL_DB_NAME": "adl_event",
                    "SLIMHUB_HOUSE_MAC": "house",
                },
            )
            local = FakeConnection(
                existing={
                    "battery_status": [
                        ("house", MAC, datetime(2026, 11, 2, 0, 1, 5), "76")
                    ]
                }
            )

            with patch.object(updater, "_connect", return_value=local):
                result = updater.ingest()

            self.assertEqual(result["battery_inserted"], 1)
            self.assertEqual(result["duplicates_skipped"], 1)
            query, rows = local.inserts[-1]
            self.assertIn("`battery_status`", query)
            self.assertEqual(rows, [("house", MAC, "2026-11-02 00:06:06", "77")])

    def test_battery_export_appends_new_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            paths.config_dir.mkdir(parents=True)
            (paths.config_dir / f"{MAC}.json").write_text(
                json.dumps({"address": MAC, "location": "ENTRY"}), encoding="utf-8"
            )
            stream = 0x1234
            folder = paths.data_dir / "validation" / MAC.replace(":", "")
            folder.mkdir(parents=True)
            battery_source = folder / f"{stream:016x}.jsonl"
            log_source = folder / "00000000000000aa.jsonl"
            log_source.write_text(json.dumps({"format": "VLS1"}) + "\n", encoding="utf-8")
            received = datetime(2026, 11, 2, 10, 0, 0, tzinfo=SEOUL).timestamp()
            battery_source.write_text(
                vlb1_row(MAC, stream, received, [dbat_record(stream, 1, 4041, 300_000)]),
                encoding="utf-8",
            )

            first = export_battery_logs(paths)

            output = data_path(paths, "battery", date="2026-11-02")
            self.assertTrue(first["rebuild"])
            self.assertEqual(
                [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()],
                [{"device": MAC, "type": "BATTERY", "percentage": 83, "batt_mv": 4041,
                  "usb": 0, "uptime_s": 300, "timestamp": "2026-11-02 10:00:00"}],
            )

            # seq 2 arrives 20 s after its measurement; seq 1 is a retransmission.
            with battery_source.open("a", encoding="utf-8") as source:
                source.write(vlb1_row(MAC, stream, received + 320, [
                    dbat_record(stream, 1, 4041, 300_000),
                    dbat_record(stream, 2, 3743, 600_000, usb_pin=0),
                ]))
            second = export_battery_logs(paths)

            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertFalse(second["rebuild"])
            self.assertEqual(second["written"], 1)
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1])["timestamp"], "2026-11-02 10:05:00")
            self.assertEqual(json.loads(lines[1])["percentage"], 50)
            self.assertEqual(json.loads(lines[1])["usb"], 1)
            state = json.loads(battery_export_state_path(paths).read_text(encoding="utf-8"))
            self.assertEqual(state["files"][str(log_source)], -1)

    def test_battery_percentage_matches_firmware_table(self) -> None:
        self.assertEqual(percent_from_mv(4300), 100)
        self.assertEqual(percent_from_mv(4041), 83)
        self.assertEqual(percent_from_mv(3743), 50)
        self.assertEqual(percent_from_mv(3000), 0)
        self.assertEqual(percent_from_mv(2900), 0)

    def test_upload_is_skipped_without_remote_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            updater = DataDirectoryDatabaseUpdater(
                paths,
                environ={
                    "SLIMHUB_LOCAL_DB_HOST": "localhost",
                    "SLIMHUB_LOCAL_DB_USER": "local-user",
                    "SLIMHUB_LOCAL_DB_NAME": "adl_event",
                    "SLIMHUB_HOUSE_MAC": "house",
                },
            )

            with patch.object(updater, "_connect") as connect:
                result = updater.upload()

            self.assertTrue(result["skipped"])
            self.assertIn("not configured", result["reason"])
            self.assertFalse(paths.db_upload_offset_path.exists())
            self.assertTrue(updater._read_json(paths.db_upload_status_path)["ok"])
            connect.assert_not_called()

    def test_upload_copies_rows_after_cursor_and_persists_each_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            updater = DataDirectoryDatabaseUpdater(
                paths,
                environ={
                    "SLIMHUB_LOCAL_DB_HOST": "localhost",
                    "SLIMHUB_LOCAL_DB_USER": "local-user",
                    "SLIMHUB_LOCAL_DB_NAME": "adl_event",
                    "SLIMHUB_REMOTE_DB_HOST": "remote.example",
                    "SLIMHUB_REMOTE_DB_USER": "remote-user",
                    "SLIMHUB_REMOTE_DB_NAME": "adl_raw",
                    "SLIMHUB_HOUSE_MAC": "house",
                },
            )
            local = FakeConnection()
            remote = FakeConnection(fail_table="in_out")

            with patch.object(updater, "_connect", side_effect=[local, remote]):
                with self.assertRaises(RuntimeError):
                    updater.upload()

            # ADL committed before INOUT failed: its cursor is kept, INOUT is not.
            self.assertEqual(remote.inserts[0][1], [("house-a",)])
            self.assertEqual(
                updater._read_json(paths.db_upload_offset_path), {"ADL": 5}
            )
            self.assertFalse(updater._read_json(paths.db_upload_status_path)["ok"])

    def test_status_reports_ingest_and_upload_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            updater = DataDirectoryDatabaseUpdater(paths, environ={})
            updater._write_json_atomic(
                paths.db_ingest_status_path,
                {"ok": True, "result": {"adl_inserted": 1}},
            )

            status = updater.status()

            self.assertFalse(status["local_database"]["configured"])
            self.assertEqual(status["last_ingest"]["result"]["adl_inserted"], 1)


if __name__ == "__main__":
    unittest.main()
