from __future__ import annotations

import csv
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from slimhub.config import AppPaths
from slimhub.daemon import SlimHubDaemon
from slimhub.events import RawDataEvent
from slimhub.logging.legacy_report import (
    LegacyReportWriter,
    LegacyWriteItem,
    validate_legacy_report,
)
from slimhub.logging.raw_logger import CSV_FIELDS, RawDataLogger
from slimhub.protocol.nus import (
    FrameAssembler,
    RawDataPacket,
    build_frame,
    parse_frame,
)


SEOUL = ZoneInfo("Asia/Seoul")
FIXTURES = Path(__file__).parent / "fixtures" / "legacy_v2"
MAC = "90:E5:B1:D1:22:6A"
FIXED_TIMES = (
    datetime(2026, 7, 31, 10, 0, tzinfo=SEOUL).timestamp(),
    datetime(2026, 7, 31, 10, 6, tzinfo=SEOUL).timestamp(),
)

DEBUG_DOCUMENT = {
    "device": MAC,
    "type": "DEBUG",
    "event": "ENTER",
    "value": 10,
}
INFERENCE_DOCUMENT = {
    "device": MAC,
    "type": "INFERENCE",
    "ADL": "shower",
    "status": "COMPLETE",
    "sequence": "D0_S6_E1_D1_",
    "sequence_list": "flushing_end",
    "truth": 0.98,
    "missing": "",
}


class LegacyReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_golden_fixture_round_trip_is_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            writer = LegacyReportWriter(paths)
            fixture_documents = [
                json.loads(line)
                for line in (
                    FIXTURES / "debugstr_sample.txt"
                ).read_text(encoding="utf-8").splitlines()
            ]
            for index, (timestamp, document) in enumerate(
                zip(FIXED_TIMES, fixture_documents)
            ):
                document.pop("timestamp")
                document[f"future_{index}"] = {"allowed": True}
                report = validate_legacy_report(MAC, document)
                self.assertEqual(
                    await writer.log(
                        report,
                        timestamp=timestamp,
                        location="TOILET",
                    ),
                    "written",
                )

            debug_path = (
                paths.data_dir
                / "TOILET"
                / "DEAN_NODE_V2"
                / MAC
                / "inference"
                / "debugstr"
                / "2026-07-31.txt"
            )
            display_path = paths.display_dir / "2026-07-31.txt"
            self.assertEqual(
                debug_path.read_bytes(),
                (FIXTURES / "debugstr_sample.txt").read_bytes(),
            )
            self.assertEqual(
                display_path.read_bytes(),
                (FIXTURES / "display_sample.txt").read_bytes(),
            )

    async def test_frame_reassembly_one_two_and_low_mtu_chunks_writes_once(
        self,
    ) -> None:
        payload = json.dumps(DEBUG_DOCUMENT, separators=(",", ":")).encode()
        wire = build_frame(MAC, "REPORT", payload)
        chunk_sets = (
            [wire],
            [wire[:19], wire[19:]],
            [wire[index : index + 7] for index in range(0, len(wire), 7)],
        )
        for chunks in chunk_sets:
            with self.subTest(chunks=len(chunks)), tempfile.TemporaryDirectory() as tmpdir:
                paths = AppPaths.from_base(tmpdir)
                daemon = SlimHubDaemon(paths=paths)
                daemon.config_store.set_field(MAC, "location", "TOILET")
                assembler = FrameAssembler()
                frames = []
                for chunk in chunks:
                    frames.extend(assembler.push(chunk))
                self.assertEqual(len(frames), 1)
                with patch("slimhub.daemon.time.time", return_value=FIXED_TIMES[0]):
                    await daemon.handle_frame(MAC, parse_frame(frames[0]))

                debug = next(paths.data_dir.glob("*/*/*/inference/debugstr/*.txt"))
                self.assertEqual(len(debug.read_text(encoding="utf-8").splitlines()), 1)

    async def test_typed_and_legacy_dual_publish_each_order_writes_one_line(
        self,
    ) -> None:
        cases = (
            ("ENTER_CONFIRMED", "D0", "ENTER", 10),
            ("EXIT_CONFIRMED", "D1", "EXIT", 20),
        )
        for result, event_id, debug_event, debug_value in cases:
            typed_fields = {
                "src": "INOUT",
                "kind": "sequence",
                "event": "SEQUENCE",
                "result": result,
                "event_id": event_id,
                "boot_id": "12ab34cd",
                "cid": "17",
                "event_seq": "17",
                "event_ts_ms": "840000",
            }
            typed = parse_frame(
                build_frame(
                    MAC,
                    "REPORT",
                    ",".join(
                        f"{key}={value}" for key, value in typed_fields.items()
                    ).encode(),
                )
            )
            legacy_document = {
                **DEBUG_DOCUMENT,
                "event": debug_event,
                "value": debug_value,
            }
            legacy = parse_frame(
                build_frame(
                    MAC,
                    "REPORT",
                    json.dumps(legacy_document, separators=(",", ":")).encode(),
                )
            )
            for frames in ((typed, legacy), (legacy, typed)):
                with (
                    self.subTest(event=debug_event, order=frames[0].parsed.format),
                    tempfile.TemporaryDirectory() as tmpdir,
                ):
                    paths = AppPaths.from_base(tmpdir)
                    daemon = SlimHubDaemon(paths=paths)
                    daemon.config_store.set_field(MAC, "location", "TOILET")
                    with patch(
                        "slimhub.daemon.time.time",
                        return_value=FIXED_TIMES[0],
                    ):
                        for frame in frames:
                            await daemon.handle_frame(MAC, frame)
                        await daemon.handle_frame(MAC, legacy)
                    display = (
                        paths.display_dir / "2026-07-31.txt"
                    ).read_text(encoding="utf-8")
                    marker = f"[EVENT] - {debug_event} value: {debug_value}"
                    self.assertEqual(display.count(marker), 1)
                    debug = next(
                        paths.data_dir.glob("*/*/*/inference/debugstr/*.txt")
                    )
                    self.assertEqual(
                        len(debug.read_text(encoding="utf-8").splitlines()),
                        1,
                    )

    async def test_unsynced_clock_buffers_and_flushes_without_1970_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            writer = LegacyReportWriter(paths, pending_size=2)
            debug = validate_legacy_report(MAC, DEBUG_DOCUMENT)
            inference = validate_legacy_report(MAC, INFERENCE_DOCUMENT)
            self.assertEqual(
                await writer.log(debug, timestamp=0, location="TOILET"),
                "pending_time_sync",
            )
            self.assertFalse(paths.data_dir.exists())
            await writer.log(
                inference,
                timestamp=FIXED_TIMES[1],
                location="TOILET",
            )
            self.assertFalse(list(paths.data_dir.rglob("1970-01-01.txt")))
            lines = next(paths.data_dir.glob("*/*/*/inference/debugstr/*.txt")).read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual([json.loads(line)["type"] for line in lines], ["DEBUG", "INFERENCE"])

    async def test_five_node_concurrency_keeps_locations_and_lines_isolated(
        self,
    ) -> None:
        locations = ("ENTRY", "LIVING", "BEDROOM", "KITCHEN", "TOILET")
        macs = [f"AA:BB:CC:DD:EE:{index:02X}" for index in range(1, 6)]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            writer = LegacyReportWriter(paths)

            def write_node(mac: str, location: str) -> None:
                for index in range(100):
                    document = {
                        **INFERENCE_DOCUMENT,
                        "device": mac,
                        "sequence": f"D0_S{index}_D1_",
                    }
                    frame = parse_frame(
                        build_frame(
                            mac,
                            "REPORT",
                            json.dumps(document, separators=(",", ":")).encode(),
                        )
                    )
                    writer._write_item(
                        LegacyWriteItem(
                            report=validate_legacy_report(
                                mac,
                                frame.parsed.document,
                            ),
                            timestamp=FIXED_TIMES[0],
                            location=location,
                            device_type="DEAN_NODE_V2",
                        )
                    )

            threads = [
                threading.Thread(
                    target=write_node,
                    args=(mac, location),
                    daemon=True,
                )
                for mac, location in zip(macs, locations)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            debug_files = sorted(
                paths.data_dir.glob("*/*/*/inference/debugstr/2026-07-31.txt")
            )
            self.assertEqual(len(debug_files), 5)
            for path in debug_files:
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(lines), 100)
                expected_mac = path.parents[2].name
                self.assertTrue(
                    all(json.loads(line)["device"] == expected_mac for line in lines)
                )

    async def test_rejects_spoof_invalid_status_and_truncated_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            daemon = SlimHubDaemon(paths=paths)
            daemon.config_store.set_field(MAC, "location", "TOILET")
            before = daemon.dean_contract.snapshot(MAC)
            spoof = {**DEBUG_DOCUMENT, "device": "11:22:33:44:55:66"}
            invalid = {**INFERENCE_DOCUMENT, "status": "INVALID"}
            invalid_value = {**DEBUG_DOCUMENT, "value": 20}
            for document in (spoof, invalid, invalid_value):
                await daemon.handle_frame(
                    MAC,
                    parse_frame(
                        build_frame(
                            MAC,
                            "REPORT",
                            json.dumps(document).encode(),
                        )
                    ),
                )
            daemon.config_store.set_field(MAC, "location", "GARAGE")
            await daemon.handle_frame(
                MAC,
                parse_frame(
                    build_frame(
                        MAC,
                        "REPORT",
                        json.dumps(DEBUG_DOCUMENT).encode(),
                    )
                ),
            )
            assembler = FrameAssembler()
            complete = build_frame(MAC, "REPORT", json.dumps(DEBUG_DOCUMENT).encode())
            self.assertEqual(assembler.push(complete[:-3]), [])
            with self.assertLogs(level="WARNING") as captured:
                assembler.clear()
            self.assertIn("truncated_on_disconnect", "\n".join(captured.output))

            self.assertEqual(before, daemon.dean_contract.snapshot(MAC))
            self.assertFalse(list(paths.data_dir.rglob("debugstr/*.txt")))
            self.assertFalse(list(paths.display_dir.glob("*.txt")))
            diagnostic = next(
                (paths.programdata_dir / "reports").glob("*.jsonl")
            ).read_text(encoding="utf-8")
            self.assertIn("device_mismatch", diagnostic)
            self.assertIn("invalid_inference_status", diagnostic)
            self.assertIn("invalid_debug_value", diagnostic)
            self.assertIn("unknown_location", diagnostic)

    async def test_raw_rows_are_exclusive_and_union_mapped(self) -> None:
        def packet(
            *,
            grid: int = 0,
            direction: int = 0,
            env: int = 0,
            sound: int = 0,
            scores: list[int] | None = None,
        ) -> RawDataPacket:
            return RawDataPacket(
                flag_human_presence=grid,
                detected=direction,
                flag_env=env,
                temperature_c=23.45,
                humidity=51,
                iaq=75,
                eco2=612,
                bvoc=13,
                accuracy=3,
                flag_sound=sound,
                sound=scores or [0] * 16,
                is_pir_human_detection_event=False,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            logger = RawDataLogger(paths)
            events = (
                RawDataEvent(FIXED_TIMES[0], MAC, "TOILET", packet(grid=1, direction=10), b""),
                RawDataEvent(FIXED_TIMES[0], MAC, "TOILET", packet(env=1), b""),
                RawDataEvent(
                    FIXED_TIMES[0],
                    MAC,
                    "TOILET",
                    packet(sound=1, scores=[-128, -128, -128, -128, 64, -128, 96] + [-128] * 9),
                    b"",
                    sound_profile="toilet_v1",
                    sound_class_count=10,
                ),
            )
            for event in events:
                await logger.write_event(event)
            path = next(paths.data_dir.glob("*/*/*/inference/rawdata/*.txt"))
            with path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 3)
            self.assertTrue(
                all(
                    sum(int(row[name]) for name in ("GridEye", "ENV", "SOUND")) == 1
                    for row in rows
                )
            )
            self.assertTrue(all(float(rows[0][label]) == 0 for label in CSV_FIELDS[10:]))
            self.assertTrue(all(float(rows[1][label]) == 0 for label in CSV_FIELDS[10:]))
            self.assertGreater(float(rows[2]["brushing"]), 0)
            self.assertGreater(float(rows[2]["flushing"]), 0)
            self.assertEqual(float(rows[2]["cooking"]), 0)

    async def test_toilet_and_else_tensors_map_to_union_columns(self) -> None:
        cases = (
            ("TOILET", "toilet_v1", 10, 4, "brushing"),
            ("KITCHEN", "kitchen_v1", 9, 4, "cooking"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            logger = RawDataLogger(paths)
            for index, (location, profile, count, slot, label) in enumerate(cases, 1):
                scores = [-128] * 16
                scores[slot] = 127
                await logger.write_event(
                    RawDataEvent(
                        FIXED_TIMES[0],
                        f"AA:BB:CC:DD:EE:{index:02X}",
                        location,
                        RawDataPacket(
                            0, 0, 0, 0, 0, 0, 0, 0, 0, 1, scores, False
                        ),
                        b"",
                        sound_profile=profile,
                        sound_class_count=count,
                    )
                )
                path = next(
                    paths.data_dir.glob(
                        f"{location}/*/*/inference/rawdata/2026-07-31.txt"
                    )
                )
                with path.open(encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    row = next(reader)
                self.assertEqual(reader.fieldnames, CSV_FIELDS)
                self.assertEqual(len(row), 24)
                self.assertGreater(float(row[label]), 0.9)
                if location == "TOILET":
                    self.assertEqual(float(row["cooking"]), 0)
                else:
                    self.assertEqual(float(row["brushing"]), 0)


if __name__ == "__main__":
    unittest.main()
