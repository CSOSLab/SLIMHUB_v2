from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from slimhub.config import AppPaths
from slimhub.integrations.database import ReportDatabaseUpdater


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
        self.connection.inserts.append((query, rows))
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


class ReportDatabaseUpdaterTests(unittest.TestCase):
    def test_accepts_deployed_legacy_database_environment_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            updater = ReportDatabaseUpdater(
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

    def test_maps_v2_raw_and_final_adl_records(self) -> None:
        records = [
            {
                "kind": "raw",
                "mac": "AA:BB:CC:DD:EE:FF",
                "location": "ENTRY",
                "receipt_ts": 0,
                "parsed": {"flag_human_presence": 1, "detected": 10},
            },
            {
                "kind": "adl_result",
                "mac": "AA:BB:CC:DD:EE:11",
                "location": "KITCHEN",
                "receipt_ts": 1,
                "final": True,
                "sequence": "S1/S2",
                "adl": "COOKING",
                "truth": 95,
            },
            {
                "kind": "adl_result",
                "mac": "AA:BB:CC:DD:EE:22",
                "location": "KITCHEN",
                "receipt_ts": 2,
                "final": False,
                "adl": "ignored",
            },
        ]

        adl_rows, inout_rows = ReportDatabaseUpdater.rows_from_records(
            records,
            house_mac="11:22:33:44:55:66",
        )

        self.assertEqual(
            inout_rows,
            [
                (
                    "11:22:33:44:55:66",
                    "ENTRY:AA:BB:CC:DD:EE:FF",
                    datetime.fromtimestamp(0).strftime("%Y-%m-%d %H:%M:%S"),
                    10,
                )
            ],
        )
        self.assertEqual(
            adl_rows,
            [
                (
                    "11:22:33:44:55:66",
                    "AA:BB:CC:DD:EE:11",
                    datetime.fromtimestamp(1).strftime("%Y-%m-%d %H:%M:%S"),
                    "S1/S2",
                    "COOKING",
                    0.95,
                )
            ],
        )

    def test_reads_only_complete_new_jsonl_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            report_path = paths.programdata_dir / "reports" / "1970-01-01.jsonl"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps({"kind": "raw", "mac": "AA", "receipt_ts": 1})
                + "\n"
                + '{"kind": "raw"',
                encoding="utf-8",
            )
            updater = ReportDatabaseUpdater(paths, environ={"SLIMHUB_DB_BACKFILL": "1"})

            records, offsets = updater._read_new_records()

            self.assertEqual(len(records), 1)
            self.assertLess(offsets[str(report_path.resolve())], report_path.stat().st_size)

            ReportDatabaseUpdater._write_json_atomic(paths.db_ingest_offset_path, offsets)
            records, offsets = updater._read_new_records()
            self.assertEqual(records, [])
            self.assertLess(offsets[str(report_path.resolve())], report_path.stat().st_size)

    def test_first_run_skips_historical_files_without_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            report_path = paths.programdata_dir / "reports" / "1970-01-01.jsonl"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps({"kind": "raw", "mac": "AA", "receipt_ts": 1}) + "\n",
                encoding="utf-8",
            )
            updater = ReportDatabaseUpdater(paths, environ={})

            records, offsets = updater._read_new_records()

            self.assertEqual(records, [])
            self.assertEqual(offsets[str(report_path.resolve())], report_path.stat().st_size)

    def test_upload_persists_each_stream_offset_after_remote_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            updater = ReportDatabaseUpdater(
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
            local = FakeConnection()
            remote = FakeConnection(fail_table="in_out")

            with patch.object(updater, "_connect", side_effect=[local, remote]):
                with self.assertRaisesRegex(RuntimeError, "simulated remote failure"):
                    updater.upload()

            offsets = ReportDatabaseUpdater._read_json(paths.db_upload_offset_path)
            self.assertEqual(offsets, {"ADL": 5})
            self.assertEqual(remote.commits, 1)
            self.assertFalse(
                ReportDatabaseUpdater._read_json(paths.db_upload_status_path)["ok"]
            )

    def test_status_reports_last_cron_update_without_connecting_to_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            updater = ReportDatabaseUpdater(
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
            ReportDatabaseUpdater._write_json_atomic(
                paths.db_status_path,
                {"ok": True, "upload": {"adl": {"uploaded": 2}}},
            )
            ReportDatabaseUpdater._write_json_atomic(
                paths.db_ingest_status_path,
                {"ok": True, "result": {"adl_inserted": 1}},
            )

            status = updater.status()

            self.assertTrue(status["local_database"]["configured"])
            self.assertTrue(status["remote_database"]["configured"])
            self.assertEqual(status["last_ingest"]["result"]["adl_inserted"], 1)
            self.assertEqual(status["last_update"], {"ok": True, "upload": {"adl": {"uploaded": 2}}})


if __name__ == "__main__":
    unittest.main()
