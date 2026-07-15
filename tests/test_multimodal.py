from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slimhub.events import ReportEvent
from slimhub.multimodal import DeploymentManifestStore, MultimodalReportStore
from slimhub.protocol.nus import ReportPacket


MAC_A = "AA:BB:CC:DD:EE:01"
MAC_B = "AA:BB:CC:DD:EE:02"


def report(
    mac: str,
    src: str,
    event: str,
    *,
    boot_id: str = "boot-a",
    event_ts_ms: int = 1000,
    location: str = "TOILET",
    **fields: object,
) -> ReportEvent:
    values = {
        "src": src,
        "event": event,
        "boot_id": boot_id,
        "event_ts_ms": str(event_ts_ms),
        **{key: str(value) for key, value in fields.items()},
    }
    message = ",".join(f"{key}={value}" for key, value in values.items())
    return ReportEvent(
        timestamp=event_ts_ms / 1000,
        receipt_timestamp=event_ts_ms / 1000,
        normalized_timestamp=event_ts_ms / 1000,
        mac=mac,
        source_address=mac,
        location=location,
        packet=ReportPacket(message, values),
        payload=message.encode(),
        wrap_epoch=0,
    )


class MultimodalStoreTests(unittest.TestCase):
    def make_store(self, manifest: dict[str, object] | None = None) -> MultimodalReportStore:
        self.tmpdir = tempfile.TemporaryDirectory()
        path = Path(self.tmpdir.name) / "deployment_manifest.json"
        if manifest is not None:
            path.write_text(json.dumps(manifest), encoding="utf-8")
        self.addCleanup(self.tmpdir.cleanup)
        return MultimodalReportStore(DeploymentManifestStore(path))

    def test_baseline_partial_mask_upserts_newer_snapshot_and_validates_fixed_profile(self) -> None:
        store = self.make_store({"nodes": {MAC_A: {"configured_profile": "toilet"}}})
        store.handle(
            report(
                MAC_A, "EVENT", "BASELINE", ready_mask="0f", required_mask="1f",
                schema=1, configured_profile="toilet", status="LEARNING", samples=20, counts="20/20/20/20/0",
            )
        )
        store.handle(
            report(
                MAC_A, "EVENT", "BASELINE", ready_mask="0f", required_mask="1f",
                schema=1, configured_profile="toilet", status="LEARNING", samples=30, counts="30/30/30/30/0",
            )
        )

        snapshot = store.snapshot()["baselines"][f"{MAC_A}/boot-a/0f"]
        self.assertEqual(snapshot["counts"], [30, 30, 30, 30, 0])
        self.assertEqual(snapshot["errors"], [])

    def test_env_and_sound_are_typed_and_never_produce_estimator_input(self) -> None:
        store = self.make_store()
        store.handle(
            report(
                MAC_A, "EVENT", "ENV", schema=1, session_seq=7, analysis_seq=51,
                event_id="E1", start_ms=100, duration_ms=20, confidence=73,
                baseline="51.0", peak="58.0", delta_levels="-2",
            )
        )
        store.handle(
            report(
                MAC_A, "EVENT", "SOUND", schema=1, session_seq=7, analysis_seq=52,
                event_id="S4", class_count=10, **{"class": 4}, count=5, max="0.941",
                mean="0.910", start_ms=120, duration_ms=50, confidence=91,
            )
        )

        records = [record for record in store.drain_records() if record.kind == "feature"]
        self.assertEqual(records[0].data["canonical_name"], "humidity")
        self.assertEqual(records[1].data["label"], "brushing")
        self.assertEqual(records[1].data["count"], 5)

    def test_sound_schema_error_is_recorded_without_crashing(self) -> None:
        store = self.make_store()
        store.handle(
            report(
                MAC_A, "EVENT", "SOUND", schema=1, session_seq=7, analysis_seq=1,
                event_id="S8", class_count=12, **{"class": 8}, count=1, max="0.9",
                mean="0.9", start_ms=10, duration_ms=10, confidence=90,
            )
        )

        feature = next(record for record in store.drain_records() if record.kind == "feature")
        self.assertIn("sound_class_count_must_be_10", feature.data["errors"])
        self.assertEqual(feature.data["label"], "watering_low")

    def test_analysis_dedupe_is_per_node_and_gaps_are_allowed(self) -> None:
        store = self.make_store()
        for mac in (MAC_A, MAC_B):
            store.handle(
                report(
                    mac, "EVENT", "ENV", schema=1, session_seq=7, analysis_seq=51,
                    event_id="E0", start_ms=1, duration_ms=1, confidence=1,
                    baseline="1", peak="2", delta_levels=1,
                )
            )
        # Retry of A/51 is ignored, but a later commit token need not be contiguous.
        store.handle(
            report(
                MAC_A, "EVENT", "ENV", schema=1, session_seq=7, analysis_seq=51,
                event_id="E0", start_ms=1, duration_ms=1, confidence=1,
                baseline="1", peak="2", delta_levels=1,
            )
        )
        store.handle(
            report(
                MAC_A, "EVENT", "ENV", schema=1, session_seq=7, analysis_seq=99,
                event_id="E2", start_ms=1, duration_ms=1, confidence=1,
                baseline="1", peak="2", delta_levels=1,
            )
        )

        records = store.drain_records()
        self.assertEqual(len([record for record in records if record.kind == "feature"]), 3)
        self.assertEqual(len([record for record in records if record.kind == "multimodal_replay"]), 1)

    def test_d0_d1_boundaries_group_features_predetect_and_final(self) -> None:
        store = self.make_store()
        store.handle_inout(
            report(MAC_A, "INOUT", "SEQUENCE", result="ENTER_CONFIRMED", event_id="D0", event_seq=41)
        )
        # Deliberately report the late-confirmed ENV after the SOUND. Physical
        # event time, not analysis_seq, determines reconstructed history order.
        store.handle(
            report(
                MAC_A, "EVENT", "SOUND", schema=1, session_seq=7, analysis_seq=52,
                event_id="S4", class_count=10, **{"class": 4}, count=5, max="0.9",
                mean="0.8", start_ms=1100, duration_ms=50, confidence=90, event_ts_ms=1200,
            )
        )
        store.handle(
            report(
                MAC_A, "EVENT", "ENV", schema=1, session_seq=7, analysis_seq=53,
                event_id="E1", start_ms=1000, duration_ms=100, confidence=70,
                baseline="1", peak="2", delta_levels=1, event_ts_ms=1100,
            )
        )
        store.handle(
            report(
                MAC_A, "ADL", "PREDETECT", schema=1, session_seq=7, analysis_seq=54,
                profile="toilet", adl="toothbrush", truth=82, progress=100, stages="2/2",
                missing=0, duration_ms=200, overflow=0, sequence="D0>S4>E1", event_ts_ms=1200,
            )
        )
        store.handle(
            report(
                MAC_A, "ADL", "COMPLETE", schema=1, session_seq=7, analysis_seq=55,
                profile="toilet", adl="toothbrush", truth=82, progress=100, stages="2/2",
                missing=0, duration_ms=300, overflow=0, sequence="D0>S4>E1>D1", event_ts_ms=1300,
            )
        )
        store.handle_inout(
            report(MAC_A, "INOUT", "SEQUENCE", result="EXIT_CONFIRMED", event_id="D1", event_seq=88, event_ts_ms=1300)
        )

        session = store.snapshot()["sessions"][f"{MAC_A}/boot-a/7"]
        self.assertEqual(session["d0"]["inout_event_seq"], 41)
        self.assertEqual(session["d1"]["inout_event_seq"], 88)
        self.assertEqual([record["event"] for record in session["records"][:2]], ["ENV", "SOUND"])
        self.assertTrue(session["predetect"]["provisional"])
        self.assertTrue(session["final"]["ground_truth_eligible"])
        self.assertFalse(
            any(record.kind == "derived_inference" for record in store.drain_records())
        )

    def test_derives_conservative_display_inference_when_firmware_final_is_absent(self) -> None:
        store = self.make_store()
        store.handle_inout(
            report(
                MAC_A,
                "INOUT",
                "SEQUENCE",
                location="BEDROOM",
                result="ENTER_CONFIRMED",
                event_id="D0",
                event_seq=41,
            )
        )
        store.handle(
            report(
                MAC_A,
                "EVENT",
                "SOUND",
                location="BEDROOM",
                schema=1,
                session_seq=41,
                analysis_seq=52,
                event_id="S2",
                class_count=10,
                **{"class": 2},
                count=5,
                max="0.91",
                mean="0.87",
                start_ms=1100,
                duration_ms=50,
                confidence=87,
                event_ts_ms=1200,
            )
        )
        store.handle_inout(
            report(
                MAC_A,
                "INOUT",
                "SEQUENCE",
                location="BEDROOM",
                result="EXIT_CONFIRMED",
                event_id="D1",
                event_seq=88,
                event_ts_ms=1300,
            )
        )

        derived = next(
            record for record in store.drain_records() if record.kind == "derived_inference"
        )
        self.assertEqual(derived.data["adl"], "watchTV")
        self.assertEqual(derived.data["sequence"], "D0_S2_D1_")
        self.assertEqual(derived.data["truth"], 0.87)
        self.assertFalse(derived.data["ground_truth_eligible"])

    def test_partial_overflow_is_final_but_not_ground_truth_and_malformed_is_safe(self) -> None:
        store = self.make_store()
        store.handle(
            report(
                MAC_A, "ADL", "PARTIAL", schema=1, session_seq=7, analysis_seq=1,
                profile="toilet", adl="x", truth="bad", progress=20, stages="bad", missing=1,
                duration_ms=30, overflow=1, sequence="truncated", event_ts_ms=1,
            )
        )
        store.handle(report(MAC_A, "EVENT", "ENV", schema="wat", session_seq=7, analysis_seq="bad", event_id="E9"))

        result = next(record for record in store.drain_records() if record.kind == "adl_result")
        self.assertTrue(result.data["final"])
        self.assertFalse(result.data["ground_truth_eligible"])


if __name__ == "__main__":
    unittest.main()
