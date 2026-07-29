from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from slimhub.events import ReportEvent
from slimhub.protocol.nus import build_frame, parse_frame
from slimhub.sound_inference import SoundInferenceStore


FIXTURE = Path(__file__).parent / "fixtures" / "sound_inference_v2_reports.json"


def report_event(mac: str, payload: str, timestamp: float = 100.0) -> ReportEvent:
    frame = parse_frame(build_frame(mac, "REPORT", payload.encode("utf-8")))
    return ReportEvent(
        timestamp=timestamp,
        mac=mac,
        source_address=mac,
        location="undefined",
        packet=frame.parsed,
        payload=frame.payload,
        receipt_timestamp=timestamp,
    )


def inference_payload(**overrides: object) -> str:
    fields: dict[str, object] = {
        "src": "SOUND",
        "event": "INFERENCE",
        "schema": 2,
        "bid": "1a2b3c4d",
        "location": "TOILET",
        "class_index": 5,
        "class_count": 10,
        "label": "flushing",
        "semantic": "flushing",
        "confidence": 0.91,
        "model": "0cb81518",
        "source": "tflm",
        "rms": 1420.5,
        "db": 61.2,
        "ts": 45000,
        "duration_ms": 1000,
    }
    fields.update(overrides)
    return ",".join(f"{key}={value}" for key, value in fields.items())


class SoundInferenceStoreTests(unittest.TestCase):
    def test_golden_reports_preserve_node_labels_without_cross_catalog_conflict(
        self,
    ) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            outcomes = [
                store.handle_report(report_event(item["mac"], item["payload"]))
                for item in fixture["reports"]
            ]

            self.assertEqual(store.inference_count(), 2)
            self.assertTrue(all(outcome.stored for outcome in outcomes))
            self.assertFalse(
                any(
                    diagnostic.kind == "sound_catalog_mismatch"
                    for outcome in outcomes
                    for diagnostic in outcome.diagnostics
                )
            )
            entries = store.snapshot()
            self.assertEqual(
                [
                    (entry["location"], entry["last_inference"]["label"])
                    for entry in entries
                ],
                [("TOILET", "flushing"), ("KITCHEN", "microwave")],
            )

    def test_rms_gate_background_and_optional_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            outcome = store.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(
                        class_index=0,
                        label="background",
                        semantic="background",
                        source="rms_gate",
                        confidence=1.0,
                    ),
                )
            )

            self.assertTrue(outcome.stored)
            self.assertEqual(outcome.diagnostics, ())
            latest = store.snapshot()[0]["last_inference"]
            self.assertEqual(latest["rms"], 1420.5)
            self.assertEqual(latest["estimated_db_spl"], 61.2)

    def test_unknown_future_label_is_lossless_and_not_adl_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            outcome = store.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(
                        class_index=7,
                        label="future_exact_Label-v3",
                        semantic="unknown",
                    ),
                )
            )

            self.assertTrue(outcome.stored)
            self.assertEqual(outcome.inference.label, "future_exact_Label-v3")
            self.assertFalse(outcome.inference.adl_eligible)

    def test_background_index_mismatch_is_stored_with_protocol_diagnostic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            for index, label, ts in (
                (0, "flushing", 1),
                (5, "background", 2),
            ):
                with self.subTest(index=index, label=label):
                    outcome = store.handle_report(
                        report_event(
                            "90:E5:10:00:00:01",
                            inference_payload(
                                class_index=index,
                                label=label,
                                semantic=label,
                                ts=ts,
                            ),
                        )
                    )
                    self.assertTrue(outcome.stored)
                    self.assertTrue(
                        any(
                            item.kind == "sound_protocol_mismatch"
                            for item in outcome.diagnostics
                        )
                    )
                    self.assertFalse(outcome.inference.adl_eligible)

    def test_class_count_boundaries_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            for count, index, bid, model in (
                (2, 1, "00000002", "aaaa0002"),
                (20, 19, "00000020", "aaaa0020"),
            ):
                with self.subTest(count=count):
                    outcome = store.handle_report(
                        report_event(
                            "90:E5:10:00:00:01",
                            inference_payload(
                                bid=bid,
                                class_count=count,
                                class_index=index,
                                label=f"class_{index}",
                                semantic=f"class_{index}",
                                model=model,
                                ts=count,
                            ),
                        )
                    )
                    self.assertTrue(outcome.stored)
            self.assertEqual(store.inference_count(), 2)

    def test_out_of_range_index_is_rejected_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            outcome = store.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(class_index=10, class_count=10),
                )
            )

            self.assertFalse(outcome.stored)
            self.assertIsNone(outcome.inference)
            self.assertIn("less than class_count", outcome.diagnostics[0].reason)
            self.assertEqual(store.inference_count(), 0)
            self.assertEqual(
                store.diagnostic_count("sound_inference_rejected"),
                1,
            )

    def test_same_catalog_index_label_change_is_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            first = store.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(ts=100, label="flushing"),
                )
            )
            changed = store.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(ts=101, label="microwave", semantic="microwave"),
                )
            )

            self.assertTrue(first.stored)
            self.assertTrue(changed.stored)
            self.assertTrue(
                any(
                    item.kind == "sound_catalog_mismatch"
                    for item in changed.diagnostics
                )
            )
            self.assertFalse(changed.inference.adl_eligible)

    def test_same_catalog_class_count_change_is_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            store.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(ts=100),
                )
            )
            changed = store.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(
                        ts=101,
                        class_count=9,
                        class_index=4,
                        label="peeing",
                        semantic="peeing",
                    ),
                )
            )

            self.assertTrue(changed.stored)
            self.assertTrue(
                any(
                    "class_count_changed" in item.reason
                    for item in changed.diagnostics
                )
            )

    def test_duplicate_report_is_stored_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            event = report_event(
                "90:E5:10:00:00:01",
                inference_payload(),
            )
            first = store.handle_report(event)
            second = store.handle_report(event)

            self.assertTrue(first.stored)
            self.assertTrue(second.duplicate)
            self.assertEqual(store.inference_count(), 1)

    def test_cached_semantic_or_model_mismatch_excludes_adl(self) -> None:
        node = SimpleNamespace(
            location="TOILET",
            class_count=10,
            model="ffffffff",
            semantic="0",
            config="READY",
            last_reason=None,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            outcome = store.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(),
                ),
                node,
            )

            self.assertTrue(outcome.stored)
            self.assertFalse(outcome.inference.adl_eligible)
            self.assertTrue(
                all(
                    item.kind == "sound_metadata_mismatch"
                    for item in outcome.diagnostics
                )
            )
            session = store.snapshot()[0]["session_metadata"]
            self.assertEqual(session["node_semantic"], "0")
            self.assertEqual(session["node_model"], "ffffffff")
            self.assertFalse(session["metadata_consistent"])

    def test_config_rejected_is_linked_and_excludes_adl(self) -> None:
        node = SimpleNamespace(
            location="TOILET",
            class_count=10,
            model="0cb81518",
            semantic="1",
            config="REJECTED",
            last_reason="model_sha_mismatch",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundInferenceStore(Path(tmpdir) / "sound.sqlite3")
            outcome = store.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(),
                ),
                node,
            )

            self.assertTrue(outcome.stored)
            self.assertFalse(outcome.inference.adl_eligible)
            self.assertTrue(
                any(
                    "CONFIG/REJECTED" in item.reason
                    for item in outcome.diagnostics
                )
            )
            session = store.snapshot()[0]["session_metadata"]
            self.assertEqual(session["node_config"], "REJECTED")
            self.assertEqual(session["node_last_reason"], "model_sha_mismatch")

    def test_old_database_is_migrated_and_reopens_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sound.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE legacy_sound(id INTEGER PRIMARY KEY, payload TEXT)"
            )
            connection.execute(
                "INSERT INTO legacy_sound(payload) VALUES('preserve-me')"
            )
            connection.commit()
            connection.close()

            first = SoundInferenceStore(path)
            first.handle_report(
                report_event(
                    "90:E5:10:00:00:01",
                    inference_payload(),
                )
            )
            first.close()
            restarted = SoundInferenceStore(path)

            self.assertEqual(restarted.inference_count(), 1)
            legacy = restarted._connection.execute(
                "SELECT payload FROM legacy_sound"
            ).fetchone()
            self.assertEqual(legacy["payload"], "preserve-me")
            restarted.close()

    def test_failed_migration_rolls_back_new_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sound.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE legacy_sound(payload TEXT)")
            connection.execute("INSERT INTO legacy_sound VALUES('preserve-me')")
            connection.execute(
                "CREATE VIEW sound_store_meta AS SELECT payload FROM legacy_sound"
            )
            connection.commit()
            connection.close()

            with self.assertRaises(sqlite3.OperationalError):
                SoundInferenceStore(path)

            connection = sqlite3.connect(path)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            value = connection.execute(
                "SELECT payload FROM legacy_sound"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(value, "preserve-me")
            self.assertNotIn("sound_inference_v2", tables)


if __name__ == "__main__":
    unittest.main()
