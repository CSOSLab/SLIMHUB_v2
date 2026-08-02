from __future__ import annotations

import tempfile
import unittest

from slimhub.config import AppPaths
from slimhub.dean_contract import (
    CONFIRM_ACK_TIMEOUT_SECONDS,
    MAX_OCCUPANCY_SECONDS,
    DeanContractStore,
)
from slimhub.events import CommandEvent, RawDataEvent, ReportEvent
from slimhub.protocol.nus import RawDataPacket, ReportPacket


MAC_A = "AA:BB:CC:DD:EE:01"
MAC_B = "AA:BB:CC:DD:EE:02"


def report(mac: str, timestamp: float, **fields: object) -> ReportEvent:
    values = {key: str(value) for key, value in fields.items()}
    message = ",".join(f"{key}={value}" for key, value in values.items())
    return ReportEvent(
        timestamp=timestamp,
        receipt_timestamp=timestamp,
        mac=mac,
        source_address=mac,
        location=values.get("location", "TOILET"),
        packet=ReportPacket(message, values),
        payload=message.encode(),
    )


def candidate(
    mac: str,
    timestamp: float,
    *,
    bid: str,
    cid: int,
    state: str,
    location: str = "TOILET",
) -> ReportEvent:
    entering = state == "in"
    return report(
        mac,
        timestamp,
        src="INOUT",
        event="ENTER" if entering else "EXIT",
        signal="enter" if entering else "exit",
        code=10 if entering else 20,
        pir=1 if entering else 0,
        radar=0,
        dist_cm=0,
        state=0,
        reason="pir_only",
        boot_id=bid,
        event_seq=cid,
        event_ts_ms=int(timestamp * 1000),
        location=location,
    )


def confirmation(
    store: DeanContractStore,
    mac: str,
    timestamp: float,
    *,
    bid: str,
    cid: int,
    rid: int,
    state: str,
    event: str = "CONFIRM_ACK",
    applied: int = 1,
    changed: int = 1,
    reason: str = "applied",
    legacy: int = 0,
) -> list[CommandEvent]:
    return store.handle_report(
        report(
            mac,
            timestamp,
            src="INOUT",
            event=event,
            schema=2,
            bid=bid,
            cid=cid,
            rid=f"{rid:x}",
            state=state,
            location="TOILET",
            source="slimhub",
            ts=int(timestamp * 1000),
            applied=applied,
            changed=changed,
            reason=reason,
            legacy=legacy,
        )
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

    def test_raw_10_20_are_evidence_and_typed_candidate_owns_confirmation(self) -> None:
        store = self.make_store()
        self.assertEqual(store.handle_raw(pir(MAC_A, 1.0, 10, "TOILET")), [])
        commands = store.handle_report(
            candidate(MAC_A, 1.1, bid="12ab34cd", cid=41, state="in")
        )

        self.assertEqual(
            [command.command for command in commands],
            [
                "inout_confirm,bid=12ab34cd,cid=41,"
                "state=in,rid=11"
            ],
        )
        self.assertEqual(store.snapshot(MAC_A)["occupancy"], None)

    def test_candidate_state_uses_signal_and_code_not_numeric_radar_state(self) -> None:
        store = self.make_store()
        event = candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        event.packet.fields["state"] = "7"

        command = store.handle_report(event)[0]

        self.assertIn("state=in", command.command)

    def test_invalid_or_duplicate_candidate_does_not_send_confirmation(self) -> None:
        store = self.make_store()
        valid = candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        self.assertEqual(len(store.handle_report(valid)), 1)
        self.assertEqual(store.handle_report(valid), [])

        invalid = candidate(MAC_A, 2.0, bid="12ab34cd", cid=42, state="in")
        invalid.packet.fields["code"] = "20"
        self.assertEqual(store.handle_report(invalid), [])

    def test_exact_confirm_ack_is_authoritative(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        )

        confirmation(
            store,
            MAC_A,
            1.1,
            bid="12ab34cd",
            cid=41,
            rid=0x11,
            state="in",
        )

        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "IN")
        self.assertEqual(store.home_snapshot()["confirmed_occupant"], MAC_A)

    def test_ack_timeout_retries_same_identity_and_already_applied_succeeds(
        self,
    ) -> None:
        store = self.make_store()
        command = store.handle_report(
            candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        )[0]
        store.handle_command_write_result(command, True, None, 1.1)

        self.assertEqual(
            store.expire_confirmations(
                1.1 + CONFIRM_ACK_TIMEOUT_SECONDS - 0.01
            ),
            [],
        )
        retry = store.expire_confirmations(
            1.1 + CONFIRM_ACK_TIMEOUT_SECONDS
        )

        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0].command, command.command)
        self.assertEqual(retry[0].cmd_id, command.cmd_id)
        store.handle_command_write_result(retry[0], True, None, 3.2)
        confirmation(
            store,
            MAC_A,
            3.3,
            bid="12ab34cd",
            cid=41,
            rid=0x11,
            state="in",
            changed=0,
            reason="already_applied",
        )

        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "IN")
        applied = [
            record
            for record in store.drain_records()
            if record.kind == "inout_confirm_applied"
        ]
        self.assertEqual(applied[-1].data["changed"], 0)
        self.assertEqual(
            applied[-1].data["status"],
            "CONFIRMED_ALREADY_APPLIED",
        )

    def test_out_ack_timeout_retry_keeps_rid_and_confirms_out(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        )
        confirmation(
            store,
            MAC_A,
            1.1,
            bid="12ab34cd",
            cid=41,
            rid=0x11,
            state="in",
        )
        command = store.handle_report(
            candidate(MAC_A, 2.0, bid="12ab34cd", cid=42, state="out")
        )[0]
        store.handle_command_write_result(command, True, None, 2.1)

        retry = store.expire_confirmations(
            2.1 + CONFIRM_ACK_TIMEOUT_SECONDS + 0.001
        )[0]
        self.assertEqual(retry.command, command.command)
        confirmation(
            store,
            MAC_A,
            4.2,
            bid="12ab34cd",
            cid=42,
            rid=0x22,
            state="out",
            changed=0,
            reason="already_applied",
        )

        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "OUT")
        self.assertIsNone(store.home_snapshot()["confirmed_occupant"])

    def test_ack_requires_cid_reason_source_applied_and_legacy_zero(self) -> None:
        cases = (
            {"reason": "already_applied"},
            {"applied": 0},
            {"legacy": 1},
        )
        for index, overrides in enumerate(cases, 1):
            with self.subTest(overrides=overrides):
                store = DeanContractStore(rid_factory=lambda: index)
                store.handle_report(
                    candidate(
                        MAC_A,
                        1.0,
                        bid="12ab34cd",
                        cid=index,
                        state="in",
                    )
                )
                confirmation(
                    store,
                    MAC_A,
                    1.1,
                    bid="12ab34cd",
                    cid=index,
                    rid=index,
                    state="in",
                    **overrides,
                )
                self.assertIsNone(store.snapshot(MAC_A)["occupancy"])

    def test_confirm_error_is_terminal_and_never_retried(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        )

        commands = confirmation(
            store,
            MAC_A,
            1.1,
            bid="12ab34cd",
            cid=41,
            rid=0x11,
            state="in",
            event="CONFIRM_ERROR",
            applied=0,
            changed=0,
            reason="stale_candidate",
        )

        self.assertEqual(commands, [])
        self.assertEqual(store.home_snapshot()["pending"], [])

    def test_request_id_conflict_terminates_same_rid_transaction(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        )

        confirmation(
            store,
            MAC_A,
            1.1,
            bid="12ab34cd",
            cid=42,
            rid=0x11,
            state="out",
            event="CONFIRM_ERROR",
            applied=0,
            changed=0,
            reason="request_id_conflict",
        )

        self.assertEqual(store.home_snapshot()["pending"], [])
        self.assertIsNone(store.snapshot(MAC_A)["occupancy"])
        self.assertIn(
            "inout_confirm_request_id_conflict",
            [record.kind for record in store.drain_records()],
        )

    def test_stale_bid_or_cid_cannot_complete_another_candidate(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        )

        confirmation(
            store,
            MAC_A,
            1.1,
            bid="12ab34ce",
            cid=41,
            rid=0x11,
            state="in",
        )
        confirmation(
            store,
            MAC_A,
            1.2,
            bid="12ab34cd",
            cid=42,
            rid=0x11,
            state="in",
        )

        self.assertEqual(len(store.home_snapshot()["pending"]), 1)
        self.assertIsNone(store.snapshot(MAC_A)["occupancy"])
        confirmation(
            store,
            MAC_A,
            1.3,
            bid="12ab34cd",
            cid=41,
            rid=0x11,
            state="in",
        )
        self.assertEqual(store.snapshot(MAC_A)["occupancy"], "IN")

    def test_new_node_boot_invalidates_old_pending_candidate_without_retry(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        )
        store.handle_report(
            report(
                MAC_A,
                1.1,
                src="NODE",
                event="STATUS",
                bid="12ab34ce",
                occupancy="OUT",
                authority="slimhub_confirmed",
            )
        )

        self.assertEqual(store.home_snapshot()["pending"], [])
        self.assertIn(
            "inout_confirm_stale",
            [record.kind for record in store.drain_records()],
        )

    def test_exit_ack_and_legacy_barrier_release_queued_enter(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="aaaaaaaa", cid=1, state="in")
        )
        confirmation(
            store,
            MAC_A,
            1.1,
            bid="aaaaaaaa",
            cid=1,
            rid=0x11,
            state="in",
        )

        exit_commands = store.handle_report(
            candidate(
                MAC_B,
                2.0,
                bid="bbbbbbbb",
                cid=7,
                state="in",
                location="LIVING",
            )
        )
        self.assertEqual(
            [(command.command, command.address) for command in exit_commands],
            [("exit", MAC_A)],
        )
        store.handle_command_write_result(exit_commands[0], True, None, 2.1)
        self.assertEqual(
            confirmation(
                store,
                MAC_A,
                2.2,
                bid="aaaaaaaa",
                cid=2,
                rid=0,
                state="out",
                legacy=1,
            ),
            [],
        )
        self.assertIsNone(store.home_snapshot()["confirmed_occupant"])
        self.assertEqual(
            store.handle_report(
                report(
                    MAC_A,
                    2.3,
                    src="INOUT",
                    event="SEQUENCE",
                    result="EXIT_SYNC",
                    event_id="D1",
                    boot_id="aaaaaaaa",
                    event_seq=2,
                    event_ts_ms=2300,
                )
            ),
            [],
        )
        enter_commands = store.handle_legacy_debug_committed(
            MAC_A,
            "EXIT",
            2.4,
        )
        self.assertEqual(
            [command.command for command in enter_commands],
            [
                "inout_confirm,bid=bbbbbbbb,cid=7,"
                "state=in,rid=22"
            ],
        )

    def test_handoff_already_out_ack_skips_legacy_barrier(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="aaaaaaaa", cid=1, state="in")
        )
        confirmation(
            store,
            MAC_A,
            1.1,
            bid="aaaaaaaa",
            cid=1,
            rid=0x11,
            state="in",
        )
        exit_command = store.handle_report(
            candidate(
                MAC_B,
                2.0,
                bid="bbbbbbbb",
                cid=7,
                state="in",
                location="LIVING",
            )
        )[0]
        store.handle_command_write_result(exit_command, True, None, 2.1)

        enter_commands = confirmation(
            store,
            MAC_A,
            2.2,
            bid="aaaaaaaa",
            cid=2,
            rid=0,
            state="out",
            changed=0,
            reason="already_applied",
            legacy=1,
        )

        self.assertEqual(len(enter_commands), 1)
        self.assertIn("state=in,rid=22", enter_commands[0].command)
        handoff = store.home_snapshot()["handoff"]
        self.assertEqual(handoff["status"], "WAIT_B_ENTER_ACK")
        self.assertFalse(handoff["exit_sync_seen"])
        self.assertFalse(handoff["exit_legacy_committed"])

    def test_handoff_exit_ack_timeout_preserves_a_and_pending_b(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="aaaaaaaa", cid=1, state="in")
        )
        confirmation(
            store,
            MAC_A,
            1.1,
            bid="aaaaaaaa",
            cid=1,
            rid=0x11,
            state="in",
        )
        command = store.handle_report(
            candidate(MAC_B, 2.0, bid="bbbbbbbb", cid=7, state="in")
        )[0]
        for attempt in range(3):
            sent_at = 3.0 + attempt * 3.0
            store.handle_command_write_result(command, True, None, sent_at)
            retries = store.expire_confirmations(
                sent_at + CONFIRM_ACK_TIMEOUT_SECONDS
            )
            if attempt < 2:
                self.assertEqual(
                    [(item.command, item.address) for item in retries],
                    [("exit", MAC_A)],
                )
                command = retries[0]
            else:
                self.assertEqual(retries, [])

        home = store.home_snapshot()
        self.assertEqual(home["confirmed_occupant"], MAC_A)
        self.assertEqual(home["queued_in"], MAC_B)
        self.assertEqual(
            home["handoff"]["status"],
            "RECONCILIATION_REQUIRED",
        )
        self.assertEqual(home["pending"], [])

    def test_failed_gatt_write_preserves_pending_identity_for_retry(self) -> None:
        store = self.make_store()
        command = store.handle_report(
            candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        )[0]

        store.handle_command_write_result(
            command,
            False,
            "link lost",
            1.1,
        )

        pending = store.home_snapshot()["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["rid"], "11")
        self.assertEqual(pending[0]["status"], "CANDIDATE_RECEIVED")
        self.assertIn(
            "inout_confirm_write_failed",
            [record.kind for record in store.drain_records()],
        )

    def test_timeout_cannot_invent_confirmation_without_candidate(self) -> None:
        store = self.make_store()
        store.handle_report(
            candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
        )
        confirmation(
            store,
            MAC_A,
            1.1,
            bid="12ab34cd",
            cid=41,
            rid=0x11,
            state="in",
        )

        self.assertEqual(
            store.expire_occupancy(1.0 + MAX_OCCUPANCY_SECONDS),
            [],
        )

    def test_config_cache_and_home_token_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            store = DeanContractStore(paths.node_state_path, rid_factory=lambda: 1)
            store.handle_report(
                candidate(MAC_A, 1.0, bid="12ab34cd", cid=41, state="in")
            )
            confirmation(
                store,
                MAC_A,
                1.1,
                bid="12ab34cd",
                cid=41,
                rid=1,
                state="in",
            )

            restored = DeanContractStore(paths.node_state_path)

            self.assertEqual(restored.home_snapshot()["confirmed_occupant"], MAC_A)
            self.assertEqual(restored.snapshot(MAC_A)["bid"], "12ab34cd")


if __name__ == "__main__":
    unittest.main()
