from __future__ import annotations

import unittest

from slimhub.ble.registry import DeviceRegistry
from slimhub.events import CommandEvent


class FakeSession:
    def __init__(self, address: str) -> None:
        self.address = address
        self.commands: list[CommandEvent] = []

    async def send_command(self, command: CommandEvent) -> None:
        self.commands.append(command)

    def status(self) -> dict[str, object]:
        return {"address": self.address}

    async def stop(self) -> None:
        return None


class DeviceRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_alias_routes_packet_mac_to_ble_session(self) -> None:
        registry = DeviceRegistry()
        session = FakeSession("AA:BB:CC:DD:EE:01")
        await registry.add(session)
        await registry.register_alias("11:22:33:44:55:66", session.address)

        sent = await registry.send_command(
            CommandEvent("11:22:33:44:55:66", "strong_enter", "ENTRY", "test")
        )

        self.assertTrue(sent)
        self.assertEqual(session.commands[0].command, "strong_enter")
        self.assertEqual(
            await registry.resolve_address("11:22:33:44:55:66"),
            "AA:BB:CC:DD:EE:01",
        )


if __name__ == "__main__":
    unittest.main()
