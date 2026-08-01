from __future__ import annotations

import tempfile
import unittest

from slimhub.config import AppPaths
from slimhub.dean_contract import MAX_OCCUPANCY_SECONDS, DeanContractStore
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


def pir(mac: str, timestamp: float, detected: int, location: str) -> RawDataEvent:
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
        is_pir_human_detection_event=True,
    )
    return RawDataEvent(timestamp, mac, location, packet, b"\x00" * 33)


class DeanContractTests(unittest.TestCase):
    def make_store(self) -> DeanContractStore:
        rids = iter((0x11, 0x22, 0x33, 0x44, 0x55, 0x66))
        return DeanContractStore(rid_factory=lambda: next(rids))

    @staticmethod
    def status(
        store: DeanContractStore,
        mac: str,
        *,
        bid: str,
        occupancy: str = "OUT",
        location: str = "TOILET",
    ) -> list:
        return store.handle_report(
            report(
                mac,
                1.0,
                src="NODE",
                event="STATUS",
                schema=2,
                bid=bid,
                location=location,
                source="node",
                occupancy=occupancy,
                authority="slimhub",
                capture="IDLE",
                config="READY",
                semantic=1,
                class_count=10,
                profile="toilet_v1",
                model="cafebabe",
                commands=0,
            )
        )

    @staticmethod
    def sync_result(
        store: DeanContractStore,
        mac: str,
        timestamp: float,
        *,
        bid: str,
        rid: int,
        state: str,
        event: str = "SYNC_ACK",
        applied: int = 1,
        changed: int = 1,
        reason: str = "applied",
    ) -> list:
        return store.handle_report(
            report(
                mac,
                timestamp,
                src="INOUT",
                event=event,
                schema=2,
                bid=bid,
                rid=f"{rid:x}",
                state=state,
                location="TOILET",
                source="slimhub",
                ts=1000,
                applied=applied,
                changed=changed,
                reason=reason,
            )
        )

    def test_pir_zero_is_observation_only_and_one_assigns_token(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A, bid="aaa1")

        self.assertEqual(store.handle_raw(pir(MAC_A, 2.0, 0, "TOILET")), [])
        commands = store.handle_raw(pir(MAC_A, 3.0, 1, "TOILET"))

        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0].command,
            "inout_sync,bid=aaa1,state=in,rid=11",
        )
        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "OUT")

    def test_home_token_exits_previous_before_entering_new_node(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A, bid="aaa1")
        self.status(store, MAC_B, bid="bbb2", location="LIVING")
        store.handle_raw(pir(MAC_A, 2.0, 1, "TOILET"))
        self.sync_result(store, MAC_A, 2.1, bid="aaa1", rid=0x11, state="in")

        exit_commands = store.handle_raw(pir(MAC_B, 3.0, 1, "LIVING"))

        self.assertEqual(
            [command.command for command in exit_commands],
            ["inout_sync,bid=aaa1,state=out,rid=22"],
        )
        enter_commands = self.sync_result(
            store,
            MAC_A,
            3.1,
            bid="aaa1",
            rid=0x22,
            state="out",
        )
        self.assertEqual(
            [command.command for command in enter_commands],
            ["inout_sync,bid=bbb2,state=in,rid=33"],
        )
        self.sync_result(store, MAC_B, 3.2, bid="bbb2", rid=0x33, state="in")
        self.assertEqual(store.home_snapshot()["confirmed_occupant"], MAC_B)
        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "OUT")
        self.assertEqual(store.snapshot(MAC_B)["occupancy"], "IN")

    def test_idempotent_and_duplicate_ack_do_not_emit_transition(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A, bid="aaa1", occupancy="IN")
        store.handle_raw(pir(MAC_A, 2.0, 1, "TOILET"))

        first = self.sync_result(
            store,
            MAC_A,
            2.1,
            bid="aaa1",
            rid=0x11,
            state="in",
            changed=0,
            reason="already_applied",
        )
        duplicate = self.sync_result(
            store,
            MAC_A,
            2.2,
            bid="aaa1",
            rid=0x11,
            state="in",
            changed=0,
            reason="already_applied",
        )

        self.assertEqual(first + duplicate, [])
        self.assertEqual(store.home_snapshot()["confirmed_occupant"], MAC_A)
        self.assertEqual(
            [record.kind for record in store.drain_records()].count(
                "inout_sync_applied"
            ),
            1,
        )

    def test_stale_boot_refreshes_status_then_retries_once(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A, bid="aaa1")
        store.handle_raw(pir(MAC_A, 2.0, 1, "TOILET"))

        refresh = self.sync_result(
            store,
            MAC_A,
            2.1,
            bid="aaa1",
            rid=0x11,
            state="in",
            event="SYNC_ERROR",
            applied=0,
            changed=0,
            reason="stale_boot",
        )
        retry = self.status(store, MAC_A, bid="aaa2")

        self.assertEqual([command.command for command in refresh], ["node_status"])
        self.assertEqual(
            [command.command for command in retry],
            ["inout_sync,bid=aaa2,state=in,rid=22"],
        )

    def test_presence_during_out_transition_replaces_only_final_in_target(self) -> None:
        store = self.make_store()
        mac_c = "AA:BB:CC:DD:EE:03"
        self.status(store, MAC_A, bid="aaa1")
        self.status(store, MAC_B, bid="bbb2", location="LIVING")
        self.status(store, mac_c, bid="ccc3", location="BEDROOM")
        store.handle_raw(pir(MAC_A, 2.0, 1, "TOILET"))
        self.sync_result(store, MAC_A, 2.1, bid="aaa1", rid=0x11, state="in")
        store.handle_raw(pir(MAC_B, 3.0, 1, "LIVING"))

        replacement = store.handle_raw(pir(mac_c, 3.05, 1, "BEDROOM"))
        after_out = self.sync_result(
            store,
            MAC_A,
            3.1,
            bid="aaa1",
            rid=0x22,
            state="out",
        )

        self.assertEqual(replacement, [])
        self.assertEqual(
            [command.command for command in after_out],
            ["inout_sync,bid=ccc3,state=in,rid=33"],
        )

    def test_missing_boot_refresh_waiters_are_mac_scoped(self) -> None:
        store = self.make_store()

        first = store.handle_raw(pir(MAC_A, 2.0, 1, "TOILET"))
        second = store.handle_raw(pir(MAC_B, 2.1, 1, "LIVING"))
        retry_a = self.status(store, MAC_A, bid="aaa1")
        retry_b = self.status(store, MAC_B, bid="bbb2", location="LIVING")

        self.assertEqual([command.command for command in first], ["node_status"])
        self.assertEqual([command.command for command in second], ["node_status"])
        self.assertEqual(retry_a, [])
        self.assertEqual(
            [command.command for command in retry_b],
            ["inout_sync,bid=bbb2,state=in,rid=11"],
        )

    def test_one_hour_maximum_expires_current_token(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A, bid="aaa1")
        store.handle_raw(pir(MAC_A, 2.0, 1, "TOILET"))
        self.sync_result(store, MAC_A, 2.1, bid="aaa1", rid=0x11, state="in")

        self.assertEqual(store.expire_occupancy(2.0 + MAX_OCCUPANCY_SECONDS - 1), [])
        expired = store.expire_occupancy(2.0 + MAX_OCCUPANCY_SECONDS)

        self.assertEqual(
            [command.command for command in expired],
            ["inout_sync,bid=aaa1,state=out,rid=22"],
        )

    def test_config_cache_and_home_token_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            store = DeanContractStore(paths.node_state_path, rid_factory=lambda: 1)
            self.status(store, MAC_A, bid="aaa1")
            store.handle_raw(pir(MAC_A, 2.0, 1, "TOILET"))

            restored = DeanContractStore(paths.node_state_path, rid_factory=lambda: 2)

            self.assertEqual(restored.home_snapshot()["desired_occupant"], MAC_A)
            self.assertEqual(restored.snapshot(MAC_A)["bid"], "aaa1")

    def test_config_requires_supported_room_and_out_idle(self) -> None:
        store = self.make_store()
        self.status(store, MAC_A, bid="aaa1")
        store.validate_config_change(MAC_A, "KITCHEN", "kitchen_v1")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            store.validate_config_change(MAC_A, "ENTRY")

        self.status(store, MAC_B, bid="bbb2", occupancy="IN")
        with self.assertRaisesRegex(ValueError, "occupancy OUT"):
            store.validate_config_change(MAC_B, "TOILET")


if __name__ == "__main__":
    unittest.main()
