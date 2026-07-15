from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from slimhub.config import AppPaths
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
        if query.startswith("SHOW COLUMNS"):
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
    def __init__(self, *, fail_table: str | None = None) -> None:
        self.fail_table = fail_table
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


def data_path(paths: AppPaths, stream: str) -> Path:
    date = datetime.now().strftime("%Y-%m-%d")
    path = (
        paths.data_dir
        / "ENTRY"
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

    def test_data_reader_maps_rawdata_and_final_debugstr_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            raw = data_path(paths, "rawdata")
            raw.write_text(
                "time,GridEye,Direction,ENV\n"
                "2026-07-15 10:00:00,0,0,0\n"
                "2026-07-15 10:00:01,1,10,0\n",
                encoding="utf-8",
            )
            debug = data_path(paths, "debugstr")
            records = [
                {
                    "device": MAC,
                    "type": "INFERENCE",
                    "status": "PRE-DETECT",
                    "ADL": "pee",
                    "truth": 0.5,
                    "timestamp": "2026-07-15 10:00:01",
                },
                {
                    "device": MAC,
                    "type": "INFERENCE",
                    "status": "POP",
                    "ADL": "pee",
                    "sequence": "D0_S5_",
                    "truth": 88,
                    "timestamp": "2026-07-15 10:00:02",
                },
                {
                    "device": MAC,
                    "type": "EVENT",
                    "event": "ENTER",
                    "value": 10,
                    "timestamp": "2026-07-15 10:00:03",
                },
            ]
            debug.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            batch = DataDirectoryReader(paths).read(house_mac="11:22:33:44:55:66")

            self.assertEqual(
                batch.inout_rows,
                [("11:22:33:44:55:66", f"ENTRY:{MAC}", "2026-07-15 10:00:01", 10)],
            )
            self.assertEqual(
                batch.adl_rows,
                [
                    (
                        "11:22:33:44:55:66",
                        MAC,
                        "2026-07-15 10:00:02",
                        "D0_S5_",
                        "pee",
                        0.88,
                    )
                ],
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
            raw = data_path(paths, "rawdata")
            raw.write_text(
                "time,GridEye,Direction\n2026-07-15 10:00:00,1,20\n",
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
            local = FakeConnection()

            with patch.object(updater, "_connect", return_value=local):
                result = updater.ingest()

            self.assertEqual(result["inout_inserted"], 1)
            self.assertEqual(local.commits, 1)
            self.assertIn(str(raw.resolve()), updater._read_json(paths.db_ingest_offset_path))

    def test_preview_writes_pending_db_rows_without_db_or_offset_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            raw = data_path(paths, "rawdata")
            raw.write_text(
                "time,GridEye,Direction\n2026-07-15 10:00:00,1,20\n",
                encoding="utf-8",
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

    def test_upload_is_disabled_without_remote_connection(self) -> None:
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
                },
            )

            with patch.object(updater, "_connect") as connect:
                result = updater.upload()

            self.assertTrue(result["skipped"])
            self.assertIn("temporarily disabled", result["reason"])
            self.assertFalse(paths.db_upload_offset_path.exists())
            self.assertTrue(updater._read_json(paths.db_upload_status_path)["ok"])
            connect.assert_not_called()

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
