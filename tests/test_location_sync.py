from __future__ import annotations

import unittest

from slimhub.dean_contract import DeanContractStore
from slimhub.events import ReportEvent
from slimhub.location_sync import LocationSyncCoordinator
from slimhub.protocol.nus import ReportPacket


def report(mac: str, **fields: str) -> ReportEvent:
    message = ",".join(f"{key}={value}" for key, value in fields.items())
    return ReportEvent(
        timestamp=100.0,
        mac=mac,
        source_address=mac,
        location="undefined",
        packet=ReportPacket(message=message, fields=fields),
        payload=message.encode("utf-8"),
        connected=True,
    )


class LocationSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mac = "AA:BB:CC:DD:EE:01"
        self.contract = DeanContractStore()
        self.sync = LocationSyncCoordinator()

    def handle(self, event: ReportEvent, desired: str) -> list[object]:
        self.contract.handle_report(event)
        return self.sync.handle_report(
            event,
            self.contract.node_state(event.mac),
            desired,
        )

    def node_status(
        self,
        *,
        occupancy: str = "OUT",
        capture: str = "IDLE",
        location: str = "LIVING",
        config: str = "file_not_found",
        semantic: str = "0",
        setup: str | None = None,
    ) -> ReportEvent:
        extra = {"setup": setup} if setup is not None else {}
        return report(
            self.mac,
            src="NODE",
            event="STATUS",
            bid="1a2b3c4d",
            occupancy=occupancy,
            capture=capture,
            location=location,
            config=config,
            semantic=semantic,
            **extra,
        )

    def config_status(
        self,
        *,
        location: str = "LIVING",
        config: str = "file_not_found",
        semantic: str = "0",
    ) -> ReportEvent:
        return report(
            self.mac,
            src="CONFIG",
            event="STATUS",
            bid="1a2b3c4d",
            location=location,
            profile="living_v1",
            config=config,
            semantic=semantic,
            class_count="5",
            model="11223344",
            raw="2",
        )

    def test_valid_central_location_sends_location_only_after_both_statuses(
        self,
    ) -> None:
        self.sync.handle_connection(self.mac, True, " toilet ", 1.0)

        self.assertEqual(self.handle(self.node_status(), " toilet "), [])
        commands = self.handle(self.config_status(), " toilet ")

        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].address, self.mac)
        self.assertEqual(commands[0].command, "config_set,location=TOILET")

    def test_unknown_or_undefined_location_never_sends_config(self) -> None:
        for desired in ("Unknown", "unknown", "undefined", "", "  "):
            with self.subTest(desired=desired):
                contract = DeanContractStore()
                sync = LocationSyncCoordinator()
                sync.handle_connection(self.mac, True, desired, 1.0)
                node_event = self.node_status()
                config_event = self.config_status()
                contract.handle_report(node_event)
                self.assertEqual(
                    sync.handle_report(
                        node_event,
                        contract.node_state(self.mac),
                        desired,
                    ),
                    [],
                )
                contract.handle_report(config_event)
                self.assertEqual(
                    sync.handle_report(
                        config_event,
                        contract.node_state(self.mac),
                        desired,
                    ),
                    [],
                )

    def test_in_or_capturing_defers_then_applies_once_when_out_idle(self) -> None:
        self.sync.handle_connection(self.mac, True, "KITCHEN", 1.0)
        self.handle(
            self.node_status(occupancy="IN", capture="ACTIVE"),
            "KITCHEN",
        )
        self.assertEqual(self.handle(self.config_status(), "KITCHEN"), [])
        state = self.sync.snapshot(self.mac)
        self.assertTrue(state["deferred"])

        commands = self.handle(
            self.node_status(occupancy="OUT", capture="IDLE"),
            "KITCHEN",
        )
        duplicate = self.handle(
            self.node_status(occupancy="OUT", capture="IDLE"),
            "KITCHEN",
        )

        self.assertEqual(
            [command.command for command in commands],
            ["config_set,location=KITCHEN"],
        )
        self.assertEqual(duplicate, [])

    def test_reconnect_does_not_reapply_already_matching_configuration(self) -> None:
        for generation in range(2):
            with self.subTest(generation=generation):
                self.sync.handle_connection(self.mac, True, "TOILET", generation)
                self.assertEqual(
                    self.handle(
                        self.node_status(
                            location="TOILET",
                            config="READY",
                            semantic="1",
                        ),
                        "TOILET",
                    ),
                    [],
                )
                self.assertEqual(
                    self.handle(
                        self.config_status(
                            location="TOILET",
                            config="READY",
                            semantic="1",
                        ),
                        "TOILET",
                    ),
                    [],
                )
                self.assertTrue(self.sync.snapshot(self.mac)["synchronized"])

    def test_rejected_model_mismatch_is_terminal_for_connection(self) -> None:
        self.sync.handle_connection(self.mac, True, "TOILET", 1.0)
        self.handle(self.node_status(), "TOILET")
        self.assertEqual(len(self.handle(self.config_status(), "TOILET")), 1)
        rejected = report(
            self.mac,
            src="CONFIG",
            event="REJECTED",
            reason="model_output_count_mismatch",
            model="11223344",
            class_count="5",
        )
        self.handle(rejected, "TOILET")

        self.assertEqual(
            self.handle(
                self.node_status(occupancy="OUT", capture="IDLE"),
                "TOILET",
            ),
            [],
        )
        state = self.sync.snapshot(self.mac)
        self.assertTrue(state["terminal_error"])
        records = self.sync.drain_records()
        self.assertTrue(
            any(
                item.kind == "location_sync_rejected"
                and item.data["reason"] == "model_output_count_mismatch"
                for item in records
            )
        )

    def test_applied_class_count_mismatch_is_terminal_diagnostic(self) -> None:
        self.sync.handle_connection(self.mac, True, "TOILET", 1.0)
        self.handle(self.node_status(), "TOILET")
        self.assertEqual(len(self.handle(self.config_status(), "TOILET")), 1)
        applied = report(
            self.mac,
            src="CONFIG",
            event="APPLIED",
            location="TOILET",
            profile="toilet_v1",
            semantic="1",
            class_count="5",
            model="wrongmod",
            raw="2",
        )

        self.handle(applied, "TOILET")

        state = self.sync.snapshot(self.mac)
        self.assertTrue(state["terminal_error"])
        self.assertFalse(state["synchronized"])
        self.assertTrue(
            any(
                item.kind == "location_sync_applied_mismatch"
                and "class_count" in str(item.data["reason"])
                for item in self.sync.drain_records()
            )
        )

    def test_five_nodes_keep_target_and_response_state_isolated(self) -> None:
        contract = DeanContractStore()
        sync = LocationSyncCoordinator()
        macs = [f"AA:BB:CC:DD:EE:{index:02X}" for index in range(1, 6)]
        commands = []
        for mac in macs:
            sync.handle_connection(mac, True, "TOILET", 1.0)
            node = report(
                mac,
                src="NODE",
                event="STATUS",
                occupancy="OUT",
                capture="IDLE",
                location="LIVING",
                config="file_not_found",
                semantic="0",
            )
            config = report(
                mac,
                src="CONFIG",
                event="STATUS",
                location="LIVING",
                config="file_not_found",
                semantic="0",
            )
            for event in (node, config):
                contract.handle_report(event)
                commands.extend(
                    sync.handle_report(
                        event,
                        contract.node_state(mac),
                        "TOILET",
                    )
                )

        self.assertEqual(
            [command.address for command in commands],
            macs,
        )
        applied = report(
            macs[0],
            src="CONFIG",
            event="APPLIED",
            location="TOILET",
            profile="toilet_v1",
            semantic="1",
            class_count="10",
            model="0cb81518",
            raw="2",
        )
        contract.handle_report(applied)
        sync.handle_report(
            applied,
            contract.node_state(macs[0]),
            "TOILET",
        )

        self.assertTrue(sync.snapshot(macs[0])["synchronized"])
        for mac in macs[1:]:
            self.assertTrue(sync.snapshot(mac)["command_sent"])
            self.assertFalse(sync.snapshot(mac)["synchronized"])

    def ready_node_status(self, setup: str | None) -> ReportEvent:
        return self.node_status(
            location="KITCHEN", config="READY", semantic="1", setup=setup
        )

    def ready_config_status(self) -> ReportEvent:
        return self.config_status(location="KITCHEN", config="READY", semantic="1")

    def test_unconfigured_node_gets_config_set_even_when_location_matches(self) -> None:
        self.sync.handle_connection(self.mac, True, "KITCHEN", 1.0)

        self.assertEqual(self.handle(self.ready_node_status("unconfigured"), "KITCHEN"), [])
        commands = self.handle(self.ready_config_status(), "KITCHEN")

        self.assertEqual(
            [command.command for command in commands],
            ["config_set,location=KITCHEN"],
        )
        # Once per connection: later status reports do not repeat it.
        self.assertEqual(self.handle(self.ready_node_status("unconfigured"), "KITCHEN"), [])
        self.assertEqual(self.handle(self.ready_config_status(), "KITCHEN"), [])

    def test_unconfigured_config_set_still_waits_for_out_idle(self) -> None:
        self.sync.handle_connection(self.mac, True, "KITCHEN", 1.0)
        busy = self.node_status(
            occupancy="IN",
            capture="ACTIVE",
            location="KITCHEN",
            config="READY",
            semantic="1",
            setup="unconfigured",
        )
        self.handle(busy, "KITCHEN")
        self.assertEqual(self.handle(self.ready_config_status(), "KITCHEN"), [])
        self.assertTrue(self.sync.snapshot(self.mac)["deferred"])

        commands = self.handle(self.ready_node_status("unconfigured"), "KITCHEN")

        self.assertEqual(len(commands), 1)

    def test_configured_or_legacy_setup_values_do_not_force_config_set(self) -> None:
        for setup in (None, "waiting", "calibrating", "ready", "retry"):
            with self.subTest(setup=setup):
                sync = LocationSyncCoordinator()
                contract = DeanContractStore()
                sync.handle_connection(self.mac, True, "KITCHEN", 1.0)
                for event in (self.ready_node_status(setup), self.ready_config_status()):
                    contract.handle_report(event)
                    self.assertEqual(
                        sync.handle_report(
                            event, contract.node_state(self.mac), "KITCHEN"
                        ),
                        [],
                    )
                self.assertTrue(sync.snapshot(self.mac)["synchronized"])

    def test_unregistered_central_location_records_node_waiting_once(self) -> None:
        self.sync.handle_connection(self.mac, True, "undefined", 1.0)
        self.sync.drain_records()

        for _ in range(2):
            self.assertEqual(
                self.handle(self.ready_node_status("unconfigured"), "undefined"), []
            )
            self.assertEqual(self.handle(self.ready_config_status(), "undefined"), [])

        waiting = [
            record
            for record in self.sync.drain_records()
            if record.kind == "location_sync_awaiting_registration"
        ]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0].data["node_setup"], "unconfigured")
        self.assertEqual(waiting[0].data["reason"], "central_location_not_configurable")

        # Registering the location later releases the held config_set.
        commands = self.handle(self.ready_node_status("unconfigured"), "KITCHEN")
        self.assertEqual(
            [command.command for command in commands],
            ["config_set,location=KITCHEN"],
        )

    def test_unregistered_location_with_configured_node_records_nothing(self) -> None:
        self.sync.handle_connection(self.mac, True, "undefined", 1.0)
        self.sync.drain_records()

        self.handle(self.ready_node_status("waiting"), "undefined")
        self.handle(self.ready_config_status(), "undefined")

        self.assertEqual(self.sync.drain_records(), [])


if __name__ == "__main__":
    unittest.main()
