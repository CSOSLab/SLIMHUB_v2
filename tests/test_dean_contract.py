from __future__ import annotations

import tempfile
import unittest

from slimhub.config import AppPaths
from slimhub.dean_contract import DeanContractStore
from slimhub.events import RawDataEvent, ReportEvent
from slimhub.protocol.nus import RawDataPacket, ReportPacket


MAC_A = "AA:BB:CC:DD:EE:01"
MAC_B = "AA:BB:CC:DD:EE:02"


def report(mac: str, timestamp: float, **fields: object) -> ReportEvent:
    values = {key: str(value) for key, value in fields.items()}
    message = ",".join(f"{key}={value}" for key, value in values.items())
    return ReportEvent(
        timestamp=timestamp,
        mac=mac,
        source_address=mac,
        location=values.get("location", "TOILET"),
        packet=ReportPacket(message, values),
        payload=message.encode(),
    )


def raw(mac: str, timestamp: float, detected: int) -> RawDataEvent:
    packet = RawDataPacket(
        flag_human_presence=1,
        detected=detected,
        flag_env=0,
        temperature_c=0,
        humidity=0,
        iaq=0,
        eco2=0,
        bvoc=0,
        accuracy=0,
        flag_sound=0,
        sound=[0] * 16,
        is_pir_human_detection_event=False,
    )
    return RawDataEvent(
        timestamp=timestamp,
        mac=mac,
        location="TOILET",
        packet=packet,
        payload=b"\x00" * 33,
    )


class DeanContractTests(unittest.TestCase):
    def make_store(self) -> DeanContractStore:
        rids = iter((0x11, 0x22, 0x33, 0x44, 0x55))
        return DeanContractStore(rid_factory=lambda: next(rids))

    @staticmethod
    def status(
        store: DeanContractStore,
        mac: str,
        *,
        bid: str = "a1b2c3d4",
        authority: str = "slimhub_confirmed",
        occupancy: str = "OUT",
        capture: str = "IDLE",
    ) -> None:
        store.handle_report(
            report(
                mac,
                1.0,
                src="NODE",
                event="STATUS",
                schema=2,
                bid=bid,
                authority=authority,
                occupancy=occupancy,
                capture=capture,
                config="READY",
                semantic=1,
                class_count=10,
                profile="toilet_v1",
            )
        )

    def test_raw_candidate_never_builds_confirmation_without_bid_and_cid(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A)

        store.handle_raw(raw(MAC_A, 2.0, 10))

        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "OUT")
        record = store.drain_records()[-1]
        self.assertEqual(record.kind, "inout_raw_candidate")
        self.assertFalse(record.data["confirmable"])

    def test_two_nodes_with_same_bid_and_cid_are_independently_correlated(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A)
        self.status(store, MAC_B)

        first = store.handle_report(
            report(
                MAC_A, 2.0, src="INOUT", event="ENTER", schema=2,
                boot_id="a1b2c3d4", event_seq=7, event_ts_ms=10,
            )
        )
        second = store.handle_report(
            report(
                MAC_B, 2.1, src="INOUT", event="ENTER", schema=2,
                boot_id="a1b2c3d4", event_seq=7, event_ts_ms=11,
            )
        )

        self.assertEqual(first[0].address, MAC_A)
        self.assertEqual(second[0].address, MAC_B)
        self.assertIn("rid=11", first[0].command)
        self.assertIn("rid=22", second[0].command)

        store.handle_report(
            report(
                MAC_B, 2.2, src="INOUT", event="CONFIRM_ACK", schema=2,
                bid="a1b2c3d4", cid=7, rid=22, state="in",
                source="slimhub", applied=1,
            )
        )
        self.assertEqual(store.snapshot(MAC_B)["occupancy"], "IN")
        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "OUT")

    def test_foreign_or_delayed_ack_cannot_complete_current_waiter(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A)
        store.handle_report(
            report(
                MAC_A, 2.0, src="INOUT", event="ENTER",
                bid="a1b2c3d4", cid=7,
            )
        )

        store.handle_report(
            report(
                MAC_A, 2.1, src="INOUT", event="CONFIRM_ACK",
                bid="a1b2c3d4", cid=8, rid=11, state="in",
                source="slimhub", applied=1,
            )
        )

        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "OUT")
        self.assertEqual(store.drain_records()[-1].kind, "inout_confirmation_unmatched")

    def test_only_applied_slimhub_ack_is_authoritative(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A)
        store.handle_report(
            report(
                MAC_A, 2.0, src="INOUT", event="ENTER",
                bid="a1b2c3d4", cid=7,
            )
        )

        store.handle_report(
            report(
                MAC_A, 2.1, src="INOUT", event="CONFIRM_ACK",
                bid="a1b2c3d4", cid=7, rid=11, state="in",
                source="slimhub", applied=0, reason="state_mismatch",
            )
        )

        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "OUT")
        self.assertEqual(store.snapshot(MAC_A)["last_reason"], "state_mismatch")

    def test_no_pending_candidate_retries_once_with_new_rid(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A)
        store.handle_report(
            report(
                MAC_A, 2.0, src="INOUT", event="ENTER",
                bid="a1b2c3d4", cid=7,
            )
        )

        retry = store.handle_report(
            report(
                MAC_A, 3.0, src="INOUT", event="CONFIRM_ERROR",
                bid="a1b2c3d4", cid=7, rid=11, state="in",
                source="slimhub", applied=0, reason="no_pending_candidate",
            )
        )
        exhausted = store.handle_report(
            report(
                MAC_A, 4.0, src="INOUT", event="CONFIRM_ERROR",
                bid="a1b2c3d4", cid=7, rid=22, state="in",
                source="slimhub", applied=0, reason="no_pending_candidate",
            )
        )

        self.assertEqual(len(retry), 1)
        self.assertIn("rid=22", retry[0].command)
        self.assertEqual(exhausted, [])

    def test_reconnect_status_with_new_boot_expires_old_pending(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A)
        store.handle_report(
            report(
                MAC_A, 2.0, src="INOUT", event="EXIT",
                bid="a1b2c3d4", cid=7,
            )
        )

        self.status(store, MAC_A, bid="deadbeef")
        store.handle_report(
            report(
                MAC_A, 3.0, src="INOUT", event="CONFIRM_ACK",
                bid="a1b2c3d4", cid=7, rid=11, state="out",
                source="slimhub", applied=1,
            )
        )

        self.assertEqual(store.snapshot(MAC_A)["bid"], "deadbeef")
        self.assertEqual(store.drain_records()[-1].kind, "inout_confirmation_unmatched")

    def test_local_standalone_suppresses_confirm_but_observes_local_result(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A, authority="local_standalone")

        commands = store.handle_report(
            report(
                MAC_A, 2.0, src="INOUT", event="ENTER",
                bid="a1b2c3d4", cid=7,
            )
        )
        store.handle_report(
            report(
                MAC_A, 2.1, src="INOUT", event="CONFIRM_ACK",
                bid="a1b2c3d4", cid=7, rid=99, state="in",
                source="local", applied=1,
            )
        )

        self.assertEqual(commands, [])
        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "IN")

    def test_config_cache_changes_only_on_applied_and_requires_out_idle(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A)
        store.validate_config_change(MAC_A, "KITCHEN", "kitchen_v1")
        store.handle_report(
            report(
                MAC_A, 2.0, src="CONFIG", event="REJECTED",
                location="KITCHEN", profile="kitchen_v1", reason="busy",
            )
        )
        self.assertEqual(store.snapshot(MAC_A)["profile"], "toilet_v1")

        store.handle_report(
            report(
                MAC_A, 3.0, src="CONFIG", event="APPLIED",
                location="KITCHEN", profile="kitchen_v1", class_count=9,
                semantic=1, status="READY", model="cafebabe",
            )
        )
        self.assertEqual(store.snapshot(MAC_A)["profile"], "kitchen_v1")
        self.assertEqual(store.snapshot(MAC_A)["class_count"], 9)

        self.status(store, MAC_B, occupancy="IN")
        with self.assertRaisesRegex(ValueError, "occupancy OUT"):
            store.validate_config_change(MAC_B, "TOILET", "toilet_v1")

    def test_node_metadata_is_persisted_by_mac(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            store = DeanContractStore(paths.node_state_path, rid_factory=lambda: 1)
            self.status(store, MAC_A)

            document = paths.node_state_path.read_text(encoding="utf-8")

            self.assertIn(MAC_A, document)
            self.assertIn('"authority": "slimhub_confirmed"', document)


if __name__ == "__main__":
    unittest.main()
