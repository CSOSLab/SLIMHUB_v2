from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slimhub.events import ReportEvent
from slimhub.multimodal import DeploymentManifestStore, MultimodalReportStore
from slimhub.protocol.nus import ReportPacket, parse_report


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


def json_report(
    document: dict[str, object],
    *,
    mac: str = MAC_A,
    location: str = "TOILET",
    identity_warning: str | None = None,
) -> ReportEvent:
    message = json.dumps(document)
    fields = {
        key: str(value)
        for key, value in document.items()
        if isinstance(value, (str, int, float))
    }
    return ReportEvent(
        timestamp=1.0,
        receipt_timestamp=1.0,
        mac=mac,
        source_address=mac,
        location=location,
        packet=ReportPacket(
            message=message,
            fields=fields,
            format="json",
            document=document,
        ),
        payload=message.encode(),
        identity_warning=identity_warning,
    )


class MultimodalStoreTests(unittest.TestCase):
    def test_schema2_sound_semantics_are_location_profile_scoped(self) -> None:
        store = self.make_store()
        store.handle(
            report(
                MAC_A,
                "EVENT",
                "SOUND",
                schema=2,
                session_seq=7,
                event_ts_ms=1200,
                class_index=4,
                class_count=9,
                semantic=1,
                profile="kitchen_v1",
                location="KITCHEN",
                model="cafebabe",
            )
        )
        store.handle(
            report(
                MAC_B,
                "EVENT",
                "SOUND",
                schema=2,
                session_seq=7,
                event_ts_ms=1200,
                class_index=4,
                class_count=10,
                semantic=1,
                profile="toilet_v1",
                location="TOILET",
                model="deadbeef",
            )
        )

        features = [
            record.data
            for record in store.drain_records()
            if record.kind == "feature"
        ]

        self.assertEqual(features[0]["label"], "cooking")
        self.assertEqual(features[1]["label"], "brushing")
        self.assertEqual(features[0]["class_index"], 4)
        self.assertEqual(features[0]["model"], "cafebabe")

    def test_schema2_sound_semantic_zero_is_fail_safe_and_deduplicated(self) -> None:
        store = self.make_store()
        event = report(
            MAC_A,
            "EVENT",
            "SOUND",
            schema=2,
            session_seq=9,
            event_ts_ms=1500,
            class_index=4,
            class_count=9,
            semantic=0,
            profile="kitchen_v1",
            location="KITCHEN",
        )

        store.handle(event)
        store.handle(event)
        records = store.drain_records()
        feature = next(record for record in records if record.kind == "feature")

        self.assertIsNone(feature.data["label"])
        self.assertIn("semantic_unavailable", feature.data["errors"])
        self.assertEqual(
            sum(record.kind == "feature" for record in records),
            1,
        )
        self.assertTrue(any(record.kind == "multimodal_replay" for record in records))

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
            report(MAC_A, "INOUT", "SEQUENCE", result="EXIT_SYNC", event_id="D1", event_seq=88, event_ts_ms=1300)
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

    def test_invalid_legacy_event_value_is_not_added_to_timeline(self) -> None:
        store = self.make_store()
        document = {
            "device": MAC_A,
            "type": "EVENT",
            "event": "ENTER",
            "value": 20,
            "schema": 2,
            "bid": "12ab34cd",
            "eid": 41,
            "ts": 840000,
        }

        store.handle_legacy_json(json_report(document))

        self.assertEqual(store.snapshot()["legacy_events"], {})
        invalid = next(
            record
            for record in store.drain_records()
            if record.kind == "legacy_event_invalid"
        )
        self.assertIn("invalid_event_value", invalid.data["errors"])

    def test_json_and_typed_schema2_adl_upsert_one_canonical_activity(self) -> None:
        store = self.make_store()
        document = {
            "device": MAC_A,
            "type": "INFERENCE",
            "ADL": "pee",
            "status": "POP",
            "sequence": "D0_S5_",
            "truth": 0.88,
            "missing": "0",
            "schema": 2,
            "bid": "12ab34cd",
            "sid": 7,
            "aid": 53,
            "why": "threshold_pop",
        }
        store.handle_legacy_json(json_report(document))
        store.handle(
            report(
                MAC_A,
                "ADL",
                "POP",
                boot_id="12ab34cd",
                event_ts_ms=840000,
                schema=2,
                sid=7,
                aid=53,
                adl="pee",
                score=88,
                coverage=100,
                margin=12,
                missing=0,
                overflow=0,
                sequence="D0_S5_",
                why="threshold_pop",
            )
        )

        activities = store.snapshot()["activities"]
        self.assertEqual(len(activities), 1)
        activity = activities[f"{MAC_A}/12ab34cd/53"]
        self.assertEqual(activity["sources"], ["legacy_json", "typed_adl"])
        self.assertEqual(activity["canonical_source"], "typed_adl")
        self.assertEqual(activity["canonical_detail"]["coverage"], 100.0)

    def test_typed_schema2_compact_identity_aliases_match_json_activity_key(self) -> None:
        store = self.make_store()
        message = (
            "src=ADL,event=POP,schema=2,bid=12ab34cd,sid=7,aid=53,act=4,"
            "pop=1,ts=840000,room=toilet,adl=pee,score=88,cov=100,"
            "margin=12,m=3/3,env=2,src=3,why=threshold_pop,dur=500,"
            "rst=1,seq=D0_S5_"
        )
        packet = parse_report(message.encode())

        store.handle(
            ReportEvent(
                timestamp=840.0,
                mac=MAC_A,
                source_address=MAC_A,
                location="TOILET",
                packet=packet,
                payload=message.encode(),
            )
        )

        activity = store.snapshot()["activities"][f"{MAC_A}/12ab34cd/53"]
        self.assertEqual(activity["canonical_source"], "typed_adl")
        self.assertEqual(activity["canonical_detail"]["session_seq"], 7)
        self.assertEqual(activity["canonical_detail"]["event_ts_ms"], 840000)
        self.assertEqual(activity["canonical_detail"]["coverage"], 100.0)
        self.assertEqual(activity["canonical_detail"]["stages"], {"matched": 3, "total": 3})
        self.assertEqual(activity["canonical_detail"]["source_count"], 3)
        self.assertEqual(activity["canonical_detail"]["duration_ms"], 500)
        self.assertEqual(activity["canonical_detail"]["pop_reset"], "1")

    def test_invalid_schema2_inference_identity_and_truth_are_not_activities(self) -> None:
        store = self.make_store()
        for aid, bid, truth in ((61, "bad", 0.5), (62, "12ab34cd", 1.5)):
            store.handle_legacy_json(
                json_report(
                    {
                        "device": MAC_A,
                        "type": "INFERENCE",
                        "ADL": "pee",
                        "status": "POP",
                        "sequence": "D0_S5_",
                        "truth": truth,
                        "missing": "0",
                        "schema": 2,
                        "bid": bid,
                        "sid": 7,
                        "aid": aid,
                        "why": "threshold_pop",
                    }
                )
            )

        self.assertEqual(store.snapshot()["activities"], {})
        invalid = [
            record
            for record in store.drain_records()
            if record.kind == "legacy_inference_invalid"
        ]
        self.assertIn("invalid_bid", invalid[0].data["errors"])
        self.assertIn("invalid_truth", invalid[1].data["errors"])

    def test_legacy_inference_state_transitions_keep_pop_and_terminal_separate(self) -> None:
        store = self.make_store()
        for aid, status, why in (
            (51, "PRE-DETECT", "new_session"),
            (52, "POP", "threshold_pop"),
            (53, "COMPLETE", "d1_complete"),
        ):
            store.handle_legacy_json(
                json_report(
                    {
                        "device": MAC_A,
                        "type": "INFERENCE",
                        "ADL": "pee",
                        "status": status,
                        "sequence": "D0_S5_D1_",
                        "truth": 0.88,
                        "missing": "0",
                        "schema": 2,
                        "bid": "12ab34cd",
                        "sid": 7,
                        "aid": aid,
                        "why": why,
                    }
                )
            )

        snapshot = store.snapshot()
        self.assertEqual(len(snapshot["activities"]), 3)
        session = snapshot["sessions"][f"{MAC_A}/12ab34cd/7"]
        self.assertEqual(len(session["activity_states"]), 3)
        self.assertEqual(session["legacy_pops"][0]["event"], "POP")
        self.assertEqual(session["legacy_final"]["event"], "COMPLETE")

    def test_future_schema_and_long_sequence_preserve_raw_json(self) -> None:
        store = self.make_store()
        document = {
            "device": MAC_A,
            "type": "INFERENCE",
            "ADL": "future",
            "status": "PARTIAL",
            "sequence": "D0_" + ("E1_" * 20),
            "truth": 0.5,
            "missing": "2",
            "schema": 99,
            "bid": "12ab34cd",
            "sid": 8,
            "aid": 60,
            "why": "d1_partial",
            "future_key": {"x": 1},
        }

        store.handle_legacy_json(json_report(document))

        record = next(
            record for record in store.drain_records() if record.kind == "legacy_activity"
        )
        self.assertIn("future_or_legacy_schema", record.data["errors"])
        self.assertIn("sequence_contract_violation", record.data["errors"])
        self.assertEqual(record.data["raw_document"]["future_key"], {"x": 1})


if __name__ == "__main__":
    unittest.main()
