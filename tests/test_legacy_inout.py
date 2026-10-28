from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from slimhub.config import AppPaths
from slimhub.integrations.data_ingest import DataDirectoryReader
from slimhub.integrations.legacy_inout import (
    LegacyInOutTracker,
    LegacyRow,
    convert_debugstr,
    legacy_rows_from_rawdata,
    legacy_state_path,
)
from tests.test_daemon import SlimHubDaemon, raw_frame
from tests.test_database import RAW_HEADER, raw_row


MACS = {
    "ENTRY": "90:E5:B1:D1:23:32",
    "LIVING": "90:E5:B1:D1:23:16",
    "BEDROOM": "90:E5:B1:D1:23:26",
    "KITCHEN": "90:E5:B1:D1:23:22",
    "TOILET": "90:E5:B1:D1:23:36",
}
FROM = "2026-10-20"
LATE = datetime(2026, 12, 1)
UNTIL = "2026-12-01 00:00:00"


def inference_dir(paths: AppPaths, room: str, mac: str | None = None) -> Path:
    return paths.data_dir / room / "DEAN_NODE_V2" / (mac or MACS[room]) / "inference"


def append_raw(paths: AppPaths, room: str, time: str, direction: int = 10,
               mac: str | None = None) -> None:
    path = inference_dir(paths, room, mac) / "rawdata" / f"{time[:10]}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(RAW_HEADER, encoding="utf-8")
    with path.open("a", encoding="utf-8") as output:
        output.write(raw_row(time, 1, direction))


def node_line(room: str, time: str, event: str) -> str:
    return json.dumps({"device": MACS[room], "type": "DEBUG", "event": event,
                       "value": 10 if event == "ENTER" else 20, "timestamp": time}) + "\n"


def inference_line(room: str, time: str) -> str:
    return json.dumps({"device": MACS[room], "type": "INFERENCE", "ADL": "none",
                       "status": "NO_MATCH", "sequence": "D0_D1_", "sequence_list": "",
                       "truth": 0.0, "missing": "", "timestamp": time}) + "\n"


def events(path: Path) -> list[tuple[str, str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        document = json.loads(line)
        rows.append((document["timestamp"], document["type"],
                     document.get("event") or document.get("status")))
    return rows


class LegacyRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = AppPaths.from_base(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tracker_rules_and_state_survive_a_restart(self) -> None:
        tracker = LegacyInOutTracker(self.paths)
        self.assertEqual(tracker.enter("2026-10-21 01:00:00", "BEDROOM", MACS["BEDROOM"]),
                         [LegacyRow("2026-10-21 01:00:00", "BEDROOM", MACS["BEDROOM"], 10)])
        self.assertEqual(tracker.enter("2026-10-21 01:00:30", "BEDROOM", MACS["BEDROOM"]), [])

        restarted = LegacyInOutTracker(self.paths)  # hours later, no timeout
        self.assertEqual(restarted.enter("2026-10-21 06:30:00", "TOILET", MACS["TOILET"]), [
            LegacyRow("2026-10-21 06:30:00", "BEDROOM", MACS["BEDROOM"], 20),
            LegacyRow("2026-10-21 06:30:00", "TOILET", MACS["TOILET"], 10),
        ])

    def test_rows_from_rawdata_merge_rooms_ignore_exits_and_prefer_active_room(self) -> None:
        append_raw(self.paths, "KITCHEN", "2026-10-21 08:00:00")
        append_raw(self.paths, "KITCHEN", "2026-10-21 08:01:00", 20)   # own EXIT
        append_raw(self.paths, "TOILET", "2026-10-21 08:05:00")
        append_raw(self.paths, "TOILET", "2026-10-21 08:10:00")        # same second as LIVING
        append_raw(self.paths, "LIVING", "2026-10-21 08:10:00")
        append_raw(self.paths, "LIVING", "2026-10-22 00:00:05")        # next day, same room
        append_raw(self.paths, "BEDROOM", "2026-10-19 23:00:00")       # before from_date
        append_raw(self.paths, "_BEDROOM", "2026-10-21 09:00:00", mac=MACS["BEDROOM"])

        rows, state = legacy_rows_from_rawdata(self.paths, FROM, UNTIL)

        self.assertEqual([(r.time, r.room, r.direction) for r in rows], [
            ("2026-10-21 08:00:00", "KITCHEN", 10),
            ("2026-10-21 08:05:00", "KITCHEN", 20), ("2026-10-21 08:05:00", "TOILET", 10),
            ("2026-10-21 08:10:00", "TOILET", 20), ("2026-10-21 08:10:00", "LIVING", 10),
        ])
        self.assertEqual(state["active_room"], "LIVING")

    def test_v1_hub_output_is_reproduced_from_its_enters(self) -> None:
        """v1 rawdata of 2026-05-28 holds only the hub's mutex output.

        Feeding its ENTERs back gives the same 10/20 rows (140 of 166). v1
        bounced between two rooms within 1-3 s three times (13:29:15,
        22:32:44, 23:27:40); the rules make one handoff there, and after the
        last bounce v1's token stays on the other room until 23:34:37.
        """
        fixture = Path(__file__).parent / "fixtures" / "v1_legacy_inout_2026-05-28.csv"
        with fixture.open(encoding="utf-8") as source:
            v1 = [(row["time"], row["room"], row["mac"], int(row["direction"]))
                  for row in csv.DictReader(source)]
        # v1 carried KITCHEN over from 2026-05-27.
        append_raw(self.paths, "KITCHEN", "2026-05-27 23:00:00", mac="EA:F5:1D:19:CB:DF")
        for time, room, mac, direction in v1:
            if direction == 10:
                append_raw(self.paths, room, time, mac=mac)

        rows, _state = legacy_rows_from_rawdata(self.paths, "2026-05-27", UNTIL)
        ours = [(r.time, r.room, r.mac, r.direction) for r in rows if r.time >= "2026-05-28"]
        bounces = (("13:29:15", "13:29:16"), ("22:32:44", "22:32:44"),
                   ("23:27:40", "23:34:37"))

        def comparable(items):
            return sorted(item for item in items
                          if not any(start <= item[0][11:] <= end for start, end in bounces))

        self.assertEqual(comparable(ours), comparable(v1))


class ConvertDebugstrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = AppPaths.from_base(self.tmp.name)
        append_raw(self.paths, "KITCHEN", "2026-10-21 10:00:00")
        append_raw(self.paths, "KITCHEN", "2026-10-21 10:04:00", 20)
        append_raw(self.paths, "TOILET", "2026-10-21 10:05:00")
        append_raw(self.paths, "TOILET", "2026-10-21 10:06:00", 20)
        kitchen = inference_dir(self.paths, "KITCHEN") / "debugstr" / "2026-10-21.txt"
        toilet = inference_dir(self.paths, "TOILET") / "debugstr" / "2026-10-21.txt"
        kitchen.parent.mkdir(parents=True)
        toilet.parent.mkdir(parents=True)
        kitchen.write_text(node_line("KITCHEN", "2026-10-21 10:00:00", "ENTER")
                           + node_line("KITCHEN", "2026-10-21 10:04:00", "EXIT")
                           + inference_line("KITCHEN", "2026-10-21 10:04:01"), encoding="utf-8")
        # TOILET's ENTER line was lost on a link drop; rawdata still has it.
        toilet.write_text(node_line("TOILET", "2026-10-21 10:06:00", "EXIT")
                          + inference_line("TOILET", "2026-10-21 10:06:02"), encoding="utf-8")
        self.paths.display_dir.mkdir(parents=True)
        (self.paths.display_dir / "2026-10-21.txt").write_text(
            "2026-10-21 10:00:00  KITCHEN [EVENT] - ENTER value: 10\n"
            "2026-10-21 10:04:00  KITCHEN [EVENT] - EXIT value: 20\n"
            "2026-10-21 10:04:01  KITCHEN [INFERENCE] NO_MATCH: none, sequence: D0_D1_, truth: 0.0, missing: \n"
            "2026-10-21 10:06:00  TOILET [EVENT] - EXIT value: 20\n", encoding="utf-8")
        self.kitchen, self.toilet = kitchen, toilet
        self.paths.db_ingest_offset_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.db_ingest_offset_path.write_text(
            json.dumps({str(kitchen.resolve()): 999, "other": 5}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dry_run_writes_nothing(self) -> None:
        before = self.kitchen.read_bytes()
        result = convert_debugstr(self.paths, FROM, now=LATE, dry_run=True)
        self.assertEqual((result["legacy_rows"], result["node_lines_moved"]), (3, 3))
        self.assertEqual(self.kitchen.read_bytes(), before)
        self.assertFalse(legacy_state_path(self.paths).exists())

    def test_conversion_moves_node_lines_and_merges_legacy_lines(self) -> None:
        result = convert_debugstr(self.paths, FROM, now=LATE)

        self.assertEqual(events(self.kitchen), [
            ("2026-10-21 10:00:00", "EVENT", "ENTER"),
            ("2026-10-21 10:04:01", "INFERENCE", "NO_MATCH"),
            ("2026-10-21 10:05:00", "EVENT", "EXIT"),
        ])
        self.assertEqual(events(self.toilet), [
            ("2026-10-21 10:05:00", "EVENT", "ENTER"),
            ("2026-10-21 10:06:02", "INFERENCE", "NO_MATCH"),
        ])
        moved = inference_dir(self.paths, "KITCHEN") / "new_algo_inout" / "2026-10-21.txt"
        self.assertEqual([e[2] for e in events(moved)], ["ENTER", "EXIT"])
        self.assertEqual(
            (self.paths.display_dir / "2026-10-21.txt").read_text(encoding="utf-8").splitlines(),
            ["2026-10-21 10:00:00  KITCHEN [EVENT] - ENTER value: 10",
             "2026-10-21 10:04:01  KITCHEN [INFERENCE] NO_MATCH: none, sequence: D0_D1_, truth: 0.0, missing: ",
             "2026-10-21 10:05:00  KITCHEN [EVENT] - EXIT value: 20",
             "2026-10-21 10:05:00  TOILET [EVENT] - ENTER value: 10"])
        # Originals backed up, rewritten offsets cleared, live token continues.
        backup = Path(result["backup"])
        self.assertTrue((backup / self.kitchen.relative_to(self.paths.base_dir)).exists())
        self.assertEqual(json.loads(self.paths.db_ingest_offset_path.read_text()), {"other": 5})
        self.assertEqual(json.loads(legacy_state_path(self.paths).read_text())["active_room"],
                         "TOILET")
        with self.assertRaises(RuntimeError):
            convert_debugstr(self.paths, FROM, now=LATE)

        # The unchanged DB sync rule now reads the legacy rows from debugstr.
        batch = DataDirectoryReader(self.paths).read(house_mac="house")
        self.assertEqual(sorted(batch.inout_rows), [
            ("house", f"KITCHEN:{MACS['KITCHEN']}", "2026-10-21 10:00:00", 10),
            ("house", f"KITCHEN:{MACS['KITCHEN']}", "2026-10-21 10:05:00", 20),
            ("house", f"TOILET:{MACS['TOILET']}", "2026-10-21 10:05:00", 10),
        ])


class DaemonLegacyInOutTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_enter_writes_legacy_event_lines_to_each_room(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            daemon = SlimHubDaemon(paths=paths)
            daemon.config_store.set_field(MACS["KITCHEN"], "location", "KITCHEN")
            daemon.config_store.set_field(MACS["TOILET"], "location", "TOILET")
            moments = iter([1793151166.0, 1793151170.0, 1793151180.0, 1793151190.0])
            with patch("slimhub.daemon.time.time", side_effect=lambda: next(moments)):
                await daemon.handle_frame(MACS["KITCHEN"], raw_frame(MACS["KITCHEN"], 10))
                await daemon.handle_frame(MACS["KITCHEN"], raw_frame(MACS["KITCHEN"], 20))
                await daemon.handle_frame(MACS["TOILET"], raw_frame(MACS["TOILET"], 10))
            await daemon.legacy_report_writer.stop()

            kitchen = next(paths.data_dir.glob("KITCHEN/*/*/inference/debugstr/*.txt"))
            toilet = next(paths.data_dir.glob("TOILET/*/*/inference/debugstr/*.txt"))
            self.assertEqual([(e[1], e[2]) for e in events(kitchen)],
                             [("EVENT", "ENTER"), ("EVENT", "EXIT")])
            self.assertEqual([(e[1], e[2]) for e in events(toilet)], [("EVENT", "ENTER")])
            self.assertEqual(events(kitchen)[1][0], events(toilet)[0][0])
            self.assertEqual(LegacyInOutTracker(paths).state["active_room"], "TOILET")


if __name__ == "__main__":
    unittest.main()
