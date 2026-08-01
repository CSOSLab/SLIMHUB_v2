from __future__ import annotations

import unittest

from slimhub.events import RawDataEvent, ReportEvent, UnitspaceSignalEvent
from slimhub.protocol.nus import RawDataPacket, ReportPacket
from slimhub.unitspace import SimpleUnitspaceEstimator, inout_report_action
from slimhub.unitspace.clock import EventReorderBuffer, NodeClockNormalizer


def make_event(address: str, location: str, timestamp: float, detected: int = 10) -> RawDataEvent:
    packet = RawDataPacket(
        flag_human_presence=1,
        detected=detected,
        flag_env=0,
        temperature_c=0.0,
        humidity=0,
        iaq=0,
        eco2=0,
        bvoc=0,
        accuracy=0,
        flag_sound=0,
        sound=[0] * 16,
        is_pir_human_detection_event=detected == 1,
    )
    return RawDataEvent(timestamp, address, location, packet, b"")


def inout_report(
    address: str,
    *,
    event: str = "ENTER",
    event_id: str = "NONE",
    result: str | None = None,
    boot_id: str = "boot-a",
    primary_seq: int | None = None,
    event_seq: int | None = 41,
    timestamp: float = 10.1,
) -> ReportEvent:
    fields = {
        "src": "INOUT",
        "event": event,
        "boot_id": boot_id,
        "event_id": event_id,
        "event_ts_ms": "123456",
    }
    if event == "ENTER":
        fields.update({"signal": "enter", "code": "10"})
    if result is not None:
        fields["result"] = result
    if primary_seq is not None:
        fields["primary_seq"] = str(primary_seq)
    if event_seq is not None:
        fields["event_seq"] = str(event_seq)
    message = ",".join(f"{key}={value}" for key, value in fields.items())
    return ReportEvent(
        timestamp=timestamp,
        receipt_timestamp=timestamp,
        mac=address,
        source_address=address,
        location="ENTRY",
        packet=ReportPacket(message, fields),
        payload=message.encode(),
    )


class UnitspaceTests(unittest.TestCase):
    def test_raw10_and_legacy_sidecar_never_assign_demo_occupancy(self) -> None:
        estimator = SimpleUnitspaceEstimator()

        first = estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0))
        sidecar = estimator.handle_report(inout_report("AA:BB:CC:DD:EE:01"))

        self.assertEqual(first, [])
        self.assertEqual(sidecar, [])

    def test_lost_sidecar_raw10_falls_back_once_and_late_sidecar_is_not_movement(self) -> None:
        estimator = SimpleUnitspaceEstimator()

        first = estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0))
        late_sidecar = estimator.handle_report(
            inout_report("AA:BB:CC:DD:EE:01", timestamp=13.0)
        )

        self.assertEqual(first, [])
        self.assertEqual(late_sidecar, [])

    def test_same_node_repeated_candidate_updates_evidence_without_command_spam(self) -> None:
        estimator = SimpleUnitspaceEstimator()
        estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0))

        commands = estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.2))

        self.assertEqual(commands, [])
        self.assertIsNone(estimator.snapshot()["desired_address"])

    def test_new_candidate_enters_new_node_then_exits_previous(self) -> None:
        estimator = SimpleUnitspaceEstimator()
        estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0))
        estimator.handle_report(
            inout_report(
                "AA:BB:CC:DD:EE:01",
                event="SEQUENCE",
                result="ENTER_CONFIRMED",
                event_id="D0",
            )
        )

        commands = estimator.handle(make_event("AA:BB:CC:DD:EE:02", "LIVING", 12.0))

        self.assertEqual(commands, [])
        self.assertIsNone(estimator.snapshot()["desired_address"])

    def test_raw20_exits_only_the_current_desired_node(self) -> None:
        estimator = SimpleUnitspaceEstimator()
        a = "AA:BB:CC:DD:EE:01"
        b = "AA:BB:CC:DD:EE:02"
        estimator.handle(make_event(a, "ENTRY", 10.0))

        unrelated_exit = estimator.handle(
            make_event(b, "LIVING", 10.5, detected=20)
        )
        current_exit = estimator.handle(
            make_event(a, "ENTRY", 11.0, detected=20)
        )

        self.assertEqual(unrelated_exit, [])
        self.assertEqual(current_exit, [])
        self.assertIsNone(estimator.snapshot()["desired_address"])

    def test_late_raw20_from_previous_node_cannot_evict_new_occupant(self) -> None:
        estimator = SimpleUnitspaceEstimator()
        a = "AA:BB:CC:DD:EE:01"
        b = "AA:BB:CC:DD:EE:02"
        estimator.handle(make_event(a, "ENTRY", 10.0))
        estimator.handle(make_event(b, "LIVING", 11.0))

        commands = estimator.handle(
            make_event(a, "ENTRY", 12.0, detected=20)
        )

        self.assertEqual(commands, [])
        self.assertIsNone(estimator.snapshot()["desired_address"])
        self.assertIsNone(estimator.snapshot()["last_location"])

    def test_d0_d1_transitions_set_confirmed_shadow_without_feedback_commands(self) -> None:
        estimator = SimpleUnitspaceEstimator()
        estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0))
        estimator.handle(make_event("AA:BB:CC:DD:EE:02", "LIVING", 12.0))

        d0 = estimator.handle_report(
            inout_report(
                "AA:BB:CC:DD:EE:02",
                event="SEQUENCE",
                result="ENTER_CONFIRMED",
                event_id="D0",
                boot_id="boot-b",
            )
        )
        d1 = estimator.handle_report(
            inout_report(
                "AA:BB:CC:DD:EE:01",
                event="SEQUENCE",
                result="EXIT_CONFIRMED",
                event_id="D1",
                boot_id="boot-a",
            )
        )

        self.assertEqual(d0 + d1, [])
        self.assertEqual(estimator.snapshot()["confirmed_occupants"], ["AA:BB:CC:DD:EE:02"])

    def test_c0_noop_is_ack_but_does_not_confirm_or_create_command(self) -> None:
        estimator = SimpleUnitspaceEstimator()

        commands = estimator.handle_report(
            inout_report(
                "AA:BB:CC:DD:EE:01",
                event="EVENT",
                event_id="C0",
                primary_seq=1,
                event_seq=None,
            )
        )

        snapshot = estimator.snapshot()
        self.assertEqual(commands, [])
        self.assertEqual(snapshot["acks"]["AA:BB:CC:DD:EE:01"]["event_id"], "C0")
        self.assertEqual(snapshot["confirmed_occupants"], [])

    def test_sidecar_exact_dedupe_and_per_node_interleave(self) -> None:
        estimator = SimpleUnitspaceEstimator()
        a = inout_report("AA:BB:CC:DD:EE:01", event_seq=41)
        b = inout_report("AA:BB:CC:DD:EE:02", event_seq=41, boot_id="boot-b")

        first = estimator.handle_report(a)
        second = estimator.handle_report(b)
        duplicate = estimator.handle_report(a)

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(duplicate, [])

    def test_legacy_detected_one_flood_never_creates_strong_transition(self) -> None:
        estimator = SimpleUnitspaceEstimator()

        commands = [
            estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0 + index, detected=1))
            for index in range(20)
        ]

        self.assertTrue(all(command == [] for command in commands))
        self.assertIsNone(estimator.snapshot()["last_address"])

    def test_legacy_inout_reports_have_no_demo_action(self) -> None:
        enter = inout_report("AA:BB:CC:DD:EE:01").packet
        ack = inout_report("AA:BB:CC:DD:EE:01", event="EVENT", event_id="C0").packet

        self.assertIsNone(inout_report_action(enter))
        self.assertIsNone(inout_report_action(ack))

    def test_reorder_uses_normalized_event_time(self) -> None:
        events = [
            UnitspaceSignalEvent(10.0, "AA:BB:CC:DD:EE:02", "B", "enter", "REPORT", normalized_timestamp=20.0),
            UnitspaceSignalEvent(11.0, "AA:BB:CC:DD:EE:01", "A", "enter", "REPORT", normalized_timestamp=10.0),
        ]

        ordered = SimpleUnitspaceEstimator.reorder(events)

        self.assertEqual([event.mac for event in ordered], ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"])

    def test_different_boot_uptimes_normalize_to_central_order(self) -> None:
        clock = NodeClockNormalizer()
        # Node A has run for 1s and node B for 10 minutes; receipt time is the
        # common clock, so uptime values are never compared directly.
        a = clock.normalize("AA:BB:CC:DD:EE:01", "a", 1_000, 100.0)
        b = clock.normalize("AA:BB:CC:DD:EE:02", "b", 600_000, 101.0)
        a_next = clock.normalize("AA:BB:CC:DD:EE:01", "a", 2_000, 101.0)

        self.assertLess(a.timestamp, a_next.timestamp)
        self.assertLess(a_next.timestamp, b.timestamp + 1.0)

    def test_uptime_wrap_increments_epoch_once(self) -> None:
        clock = NodeClockNormalizer()
        clock.normalize("AA:BB:CC:DD:EE:01", "a", 0xFFFF_FFF0, 100.0)
        wrapped = clock.normalize("AA:BB:CC:DD:EE:01", "a", 20, 100.1)
        next_event = clock.normalize("AA:BB:CC:DD:EE:01", "a", 40, 100.2)

        self.assertEqual(wrapped.wrap_epoch, 1)
        self.assertEqual(next_event.wrap_epoch, 1)

    def test_reorder_buffer_restores_out_of_order_queued_reports(self) -> None:
        buffer = EventReorderBuffer[str](window_seconds=1.0)

        self.assertEqual(buffer.push("later", 12.0), [])
        self.assertEqual(buffer.push("earlier", 11.0), ["earlier"])

        self.assertEqual(buffer.flush(), ["later"])


if __name__ == "__main__":
    unittest.main()
