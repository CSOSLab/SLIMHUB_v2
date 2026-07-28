from __future__ import annotations

import tempfile
import unittest

from slimhub.config import AppPaths, DeviceConfigStore, LocationTargetError


MAC_1 = "AA:BB:CC:DD:EE:01"
MAC_2 = "AA:BB:CC:DD:EE:02"
MAC_3 = "AA:BB:CC:DD:EE:03"


class DeviceLocationTargetTests(unittest.TestCase):
    def test_unique_location_resolves_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = DeviceConfigStore(AppPaths.from_base(tmpdir))
            store.set_field(MAC_1, "location", "TOILET")

            self.assertEqual(store.resolve_target(location="toilet"), MAC_1)
            self.assertEqual(store.resolve_target(address=MAC_2), MAC_2)

    def test_initial_location_names_cannot_target_devices(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = DeviceConfigStore(AppPaths.from_base(tmpdir))
            store.set_field(MAC_1, "location", "undefined")

            for location in ("undefined", "unnamed", "unknown"):
                with self.subTest(location=location):
                    with self.assertRaisesRegex(LocationTargetError, "initial/unassigned"):
                        store.resolve_target(location=location)

    def test_duplicate_assignment_gets_next_number_and_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = DeviceConfigStore(AppPaths.from_base(tmpdir))
            store.set_field(MAC_1, "location", "TOILET")
            store.set_field(MAC_2, "location", "TOILET_2")

            config, warning = store.set_field_unique(MAC_3, "location", "TOILET")

            self.assertEqual(config.location, "TOILET_3")
            self.assertIsNotNone(warning)
            self.assertIn(MAC_1, warning or "")
            self.assertIn("TOILET_3", warning or "")

    def test_existing_duplicate_location_lists_devices_and_blocks_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = DeviceConfigStore(AppPaths.from_base(tmpdir))
            store.set_field(MAC_1, "location", "KITCHEN")
            store.set_field(MAC_2, "location", "kitchen")

            with self.assertRaises(LocationTargetError) as error:
                store.resolve_target(location="KITCHEN")

            message = str(error.exception)
            self.assertIn("duplicated", message)
            self.assertIn(MAC_1, message)
            self.assertIn(MAC_2, message)
            self.assertIn("--address <MAC>", message)


if __name__ == "__main__":
    unittest.main()
