from __future__ import annotations

import unittest

from slimhub.events import RawDataEvent
from slimhub.protocol.nus import RawDataPacket
from slimhub.unitspace import SimpleUnitspaceEstimator


def make_event(address: str, location: str, timestamp: float, detected: int = 1) -> RawDataEvent:
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


class UnitspaceTests(unittest.TestCase):
    def test_first_detection_enters_current_node(self) -> None:
        estimator = SimpleUnitspaceEstimator()

        commands = estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0))

        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].command, "enter")
        self.assertEqual(commands[0].address, "AA:BB:CC:DD:EE:01")

    def test_same_address_noise_is_ignored(self) -> None:
        estimator = SimpleUnitspaceEstimator()
        estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0))

        commands = estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 12.0))

        self.assertEqual(commands, [])
        self.assertEqual(estimator.snapshot()["last_timestamp"], 12.0)

    def test_movement_enters_new_address_and_exits_previous(self) -> None:
        estimator = SimpleUnitspaceEstimator()
        estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0))

        commands = estimator.handle(make_event("AA:BB:CC:DD:EE:02", "LIVING", 12.0))

        self.assertEqual([command.command for command in commands], ["enter", "exit"])
        self.assertEqual(commands[0].address, "AA:BB:CC:DD:EE:02")
        self.assertEqual(commands[1].address, "AA:BB:CC:DD:EE:01")
        self.assertEqual(commands[1].location, "ENTRY")

    def test_non_detected_rawdata_does_not_change_state(self) -> None:
        estimator = SimpleUnitspaceEstimator()

        commands = estimator.handle(make_event("AA:BB:CC:DD:EE:01", "ENTRY", 10.0, detected=0))

        self.assertEqual(commands, [])
        self.assertIsNone(estimator.snapshot()["last_address"])


if __name__ == "__main__":
    unittest.main()
