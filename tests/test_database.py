from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime

from slimhub.config import AppPaths
from slimhub.integrations.database import ReportDatabaseUpdater


class ReportDatabaseUpdaterTests(unittest.TestCase):
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

        adl_rows, inout_rows = ReportDatabaseUpdater.rows_from_records(records)

        self.assertEqual(
            inout_rows,
            [
                (
                    "AA:BB:CC:DD:EE:FF",
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
                    "AA:BB:CC:DD:EE:11",
                    "KITCHEN:AA:BB:CC:DD:EE:11",
                    datetime.fromtimestamp(1).strftime("%Y-%m-%d %H:%M:%S"),
                    "S1/S2",
                    "COOKING",
                    95,
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
            updater = ReportDatabaseUpdater(paths, environ={})

            records, offsets = updater._read_new_records()

            self.assertEqual(len(records), 1)
            self.assertLess(offsets[str(report_path.resolve())], report_path.stat().st_size)

            ReportDatabaseUpdater._write_json_atomic(paths.db_ingest_offset_path, offsets)
            records, offsets = updater._read_new_records()
            self.assertEqual(records, [])
            self.assertLess(offsets[str(report_path.resolve())], report_path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
