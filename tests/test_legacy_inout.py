from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.integrations.data_ingest import DataDirectoryReader, legacy_inout_line
from slimhub.integrations.legacy_inout import LegacyInOutError, LegacyInOutGenerator
from tests.test_database import RAW_HEADER, raw_row


MACS = {
    "ENTRY": "90:E5:B1:D1:23:32",
    "LIVING": "90:E5:B1:D1:23:16",
    "BEDROOM": "90:E5:B1:D1:23:26",
    "KITCHEN": "90:E5:B1:D1:23:22",
    "TOILET": "90:E5:B1:D1:23:36",
}
FROM = "2026-10-20"
LATE = datetime(2026, 12, 1)  # watermark far after every test row


def raw_path(paths: AppPaths, room: str, date: str, mac: str | None = None) -> Path:
    path = (paths.data_dir / room / "DEAN_NODE_V2" / (mac or MACS[room])
            / "inference" / "rawdata" / f"{date}.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(RAW_HEADER, encoding="utf-8")
    return path


def append(paths: AppPaths, room: str, time: str, direction: int = 10,
           grideye: int = 1, mac: str | None = None) -> None:
    with raw_path(paths, room, time[:10], mac).open("a", encoding="utf-8") as output:
        output.write(raw_row(time, grideye, direction))


def output_rows(paths: AppPaths) -> list[tuple[str, str, str, int]]:
    rows = []
    for path in sorted((paths.data_dir / "legacy_inout").glob("*.txt")):
        for line in path.read_text(encoding="utf-8").splitlines():
            time, rest = line.split("  ", 1)
            room, mac, _event, _dash, _verb, _value, value = rest.split(" ")
            rows.append((time, room, mac, int(value)))
    return rows


class LegacyInOutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = AppPaths.from_base(self.tmp.name)
        self.generator = LegacyInOutGenerator(self.paths, from_date=FROM)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rules_first_enter_same_room_handoff_and_own_exit_ignored(self) -> None:
        append(self.paths, "BEDROOM", "2026-10-21 01:00:00")
        append(self.paths, "BEDROOM", "2026-10-21 01:00:30")          # same room
        append(self.paths, "BEDROOM", "2026-10-21 01:01:00", 20)      # own EXIT
        append(self.paths, "BEDROOM", "2026-10-21 01:01:05", 0, 0)    # ENV row
        append(self.paths, "TOILET", "2026-10-21 06:30:00")           # hours later
        append(self.paths, "TOILET", "2026-10-21 06:35:00", 20)

        result = self.generator.run(now=LATE)

        self.assertEqual(output_rows(self.paths), [
            ("2026-10-21 01:00:00", "BEDROOM", MACS["BEDROOM"], 10),
            ("2026-10-21 06:30:00", "BEDROOM", MACS["BEDROOM"], 20),
            ("2026-10-21 06:30:00", "TOILET", MACS["TOILET"], 10),
        ])
        self.assertEqual(result["active_room"], "TOILET")
        self.assertEqual(result["enters"], 3)

    def test_rooms_merge_in_time_order_and_same_second_prefers_active_room(self) -> None:
        append(self.paths, "KITCHEN", "2026-10-21 08:00:00")
        append(self.paths, "TOILET", "2026-10-21 08:05:00")
        append(self.paths, "TOILET", "2026-10-21 08:10:00")
        append(self.paths, "LIVING", "2026-10-21 08:10:00")

        self.generator.run(now=LATE)

        # 08:10: TOILET is active, so its ENTER is handled first (no-op) and the
        # LIVING ENTER makes one handoff, not a TOILET→LIVING→TOILET ping-pong.
        self.assertEqual(output_rows(self.paths), [
            ("2026-10-21 08:00:00", "KITCHEN", MACS["KITCHEN"], 10),
            ("2026-10-21 08:05:00", "KITCHEN", MACS["KITCHEN"], 20),
            ("2026-10-21 08:05:00", "TOILET", MACS["TOILET"], 10),
            ("2026-10-21 08:10:00", "TOILET", MACS["TOILET"], 20),
            ("2026-10-21 08:10:00", "LIVING", MACS["LIVING"], 10),
        ])

    def test_token_crosses_midnight_into_the_next_day_file(self) -> None:
        append(self.paths, "LIVING", "2026-10-21 23:59:59")
        append(self.paths, "BEDROOM", "2026-10-22 00:00:05")

        self.generator.run(now=LATE)

        day2 = (self.paths.data_dir / "legacy_inout" / "2026-10-22.txt").read_text()
        self.assertEqual(day2, legacy_inout_line(
            "2026-10-22 00:00:05", "LIVING", MACS["LIVING"], 20) + legacy_inout_line(
            "2026-10-22 00:00:05", "BEDROOM", MACS["BEDROOM"], 10))

    def test_watermark_defers_recent_rows_and_incremental_equals_rebuild(self) -> None:
        append(self.paths, "KITCHEN", "2026-10-21 10:00:00")
        append(self.paths, "TOILET", "2026-10-21 10:00:50")
        first = self.generator.run(now=datetime(2026, 10, 21, 10, 1, 0))  # lag 60 s
        self.assertEqual(first["rows"], 1)

        append(self.paths, "LIVING", "2026-10-21 10:01:30")
        self.generator.run(now=datetime(2026, 10, 21, 10, 2, 0))
        self.generator.run(now=LATE)
        incremental = output_rows(self.paths)

        self.generator.run(now=LATE, rebuild=True)
        self.assertEqual(output_rows(self.paths), incremental)
        self.assertEqual([row[1:] for row in incremental], [
            ("KITCHEN", MACS["KITCHEN"], 10),
            ("KITCHEN", MACS["KITCHEN"], 20), ("TOILET", MACS["TOILET"], 10),
            ("TOILET", MACS["TOILET"], 20), ("LIVING", MACS["LIVING"], 10),
        ])

    def test_partial_line_waits_and_dates_before_cutover_and_other_folders_skip(self) -> None:
        append(self.paths, "KITCHEN", "2026-10-19 23:00:00")             # before cut-over
        append(self.paths, "_BEDROOM", "2026-10-21 09:00:00", mac=MACS["BEDROOM"])
        path = raw_path(self.paths, "TOILET", "2026-10-21")
        with path.open("a", encoding="utf-8") as output:
            output.write("2026-10-21 09:00:00,1,1")                       # no newline yet

        self.assertEqual(self.generator.run(now=LATE)["rows"], 0)
        with path.open("a", encoding="utf-8") as output:
            output.write("0" + ",0" * 23 + "\n")
        self.generator.run(now=LATE)
        self.assertEqual(output_rows(self.paths),
                         [("2026-10-21 09:00:00", "TOILET", MACS["TOILET"], 10)])

    def test_uncommitted_tail_is_truncated_and_from_date_change_needs_rebuild(self) -> None:
        append(self.paths, "KITCHEN", "2026-10-21 10:00:00")
        self.generator.run(now=LATE)
        day = self.paths.data_dir / "legacy_inout" / "2026-10-21.txt"
        with day.open("a", encoding="utf-8") as output:
            output.write("2026-10-21 10:00:01  KITCHEN junk\n")         # crash leftover
        self.generator.run(now=LATE)
        self.assertEqual(len(day.read_text().splitlines()), 1)

        with self.assertRaises(LegacyInOutError):
            LegacyInOutGenerator(self.paths, from_date="2026-10-21").run(now=LATE)

    def test_rebuild_backs_up_old_output_and_clears_its_ingest_offsets(self) -> None:
        append(self.paths, "KITCHEN", "2026-10-21 10:00:00")
        self.generator.run(now=LATE)
        day = self.paths.data_dir / "legacy_inout" / "2026-10-21.txt"
        self.paths.db_ingest_offset_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.db_ingest_offset_path.write_text(
            json.dumps({str(day.resolve()): 999, "other": 5}), encoding="utf-8")

        self.generator.run(now=LATE, rebuild=True)

        self.assertEqual(json.loads(self.paths.db_ingest_offset_path.read_text()),
                         {"other": 5})
        self.assertEqual(len(list(self.paths.db_sync_dir.glob(
            "legacy_inout_backup_*/2026-10-21.txt"))), 1)
        self.assertEqual(len(output_rows(self.paths)), 1)

    def test_dry_run_writes_nothing(self) -> None:
        append(self.paths, "KITCHEN", "2026-10-21 10:00:00")
        result = self.generator.run(now=LATE, dry_run=True)
        self.assertEqual(result["per_day"], {"2026-10-21": 1})
        self.assertFalse((self.paths.data_dir / "legacy_inout").exists())
        self.assertFalse(self.generator.state_path.exists())

    def test_reader_uses_legacy_file_from_cutover_and_rawdata_before(self) -> None:
        append(self.paths, "KITCHEN", "2026-10-19 09:00:00")        # before: rawdata
        append(self.paths, "KITCHEN", "2026-10-21 10:00:00")
        append(self.paths, "TOILET", "2026-10-21 10:05:00")
        append(self.paths, "TOILET", "2026-10-21 10:06:00", 20)     # own EXIT
        self.generator.run(now=LATE)
        environ = {"SLIMHUB_LEGACY_INOUT_FROM": FROM}

        batch = DataDirectoryReader(self.paths, environ=environ).read(house_mac="house")

        self.assertEqual(sorted(batch.inout_rows), [
            ("house", f"KITCHEN:{MACS['KITCHEN']}", "2026-10-19 09:00:00", 10),
            ("house", f"KITCHEN:{MACS['KITCHEN']}", "2026-10-21 10:00:00", 10),
            ("house", f"KITCHEN:{MACS['KITCHEN']}", "2026-10-21 10:05:00", 20),
            ("house", f"TOILET:{MACS['TOILET']}", "2026-10-21 10:05:00", 10),
        ])
        # Without env the cut-over comes from the generator state.
        fallback = DataDirectoryReader(self.paths, environ={}).read(house_mac="house")
        self.assertEqual(sorted(fallback.inout_rows), sorted(batch.inout_rows))

    def test_reader_without_cutover_ignores_legacy_files(self) -> None:
        append(self.paths, "KITCHEN", "2026-10-21 10:00:00")
        append(self.paths, "KITCHEN", "2026-10-21 10:06:00", 20)
        self.generator.run(now=LATE)
        self.generator.state_path.unlink()

        batch = DataDirectoryReader(self.paths, environ={}).read(house_mac="house")

        self.assertEqual([row[3] for row in sorted(batch.inout_rows)], [10, 20])

    def test_v1_hub_output_is_reproduced_from_its_enters(self) -> None:
        """v1 rawdata of 2026-05-28 holds only the hub's mutex output.

        Feeding its ENTERs back must give the same 10/20 rows (140 of 166).
        v1 bounced between two rooms within 1-3 s three times (13:29:15,
        22:32:44, 23:27:40); the generator makes one handoff there, and after
        the last bounce v1's token stays on the other room until 23:34:37.
        Those windows are excluded.
        """
        fixture = Path(__file__).parent / "fixtures" / "v1_legacy_inout_2026-05-28.csv"
        with fixture.open(encoding="utf-8") as source:
            v1 = [(row["time"], row["room"], row["mac"], int(row["direction"]))
                  for row in csv.DictReader(source)]
        for time, room, mac, direction in v1:
            if direction == 10:
                append(self.paths, room, time, mac=mac)
        generator = LegacyInOutGenerator(self.paths, from_date="2026-05-28")
        # v1 carried KITCHEN over from 2026-05-27.
        generator._save_state({
            "from_date": "2026-05-28", "active_room": "KITCHEN",
            "active_mac": "EA:F5:1D:19:CB:DF", "last_time": None,
            "offsets": {}, "outputs": {},
        })

        generator.run(now=LATE)

        bounces = (("13:29:15", "13:29:16"), ("22:32:44", "22:32:44"),
                   ("23:27:40", "23:34:37"))

        def comparable(rows):
            return sorted(row for row in rows
                          if not any(start <= row[0][11:] <= end for start, end in bounces))

        self.assertEqual(comparable(output_rows(self.paths)), comparable(v1))


if __name__ == "__main__":
    unittest.main()
