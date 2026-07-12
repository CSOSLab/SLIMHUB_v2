from __future__ import annotations

import asyncio
import sys
import types
import unittest

from slimhub.events import CommandEvent


class FakeBleakClient:
    pass


class FakeBleakError(Exception):
    pass


bleak_module = sys.modules.get("bleak") or types.ModuleType("bleak")
bleak_module.BleakClient = FakeBleakClient
sys.modules["bleak"] = bleak_module
bleak_exc_module = sys.modules.get("bleak.exc") or types.ModuleType("bleak.exc")
bleak_exc_module.BleakError = FakeBleakError
sys.modules["bleak.exc"] = bleak_exc_module

from slimhub.ble.device_session import DeviceSession


async def ignore_frame(_: str, __: object) -> None:
    return None


class FlakyClient:
    def __init__(self) -> None:
        self.attempts = 0

    async def write_gatt_char(self, *_: object, **__: object) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("link lost")


class DeviceSessionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_commands_coalesce_to_final_desired_state(self) -> None:
        session = DeviceSession("AA:BB:CC:DD:EE:01", on_frame=ignore_frame)

        await session.send_command(CommandEvent(session.address, "enter", "ENTRY", desired_epoch=1))
        await session.send_command(CommandEvent(session.address, "exit", "ENTRY", desired_epoch=2))

        self.assertEqual(session.status()["queued_commands"], 1)
        key = await session._command_queue.get()
        command = session._pending_commands.pop(key)
        self.assertEqual(command.command, "exit")
        self.assertEqual(command.desired_epoch, 2)

    async def test_failed_write_is_retried_and_remains_ack_pending(self) -> None:
        results: list[tuple[bool, str | None]] = []
        completed = asyncio.Event()

        async def on_result(
            _: CommandEvent,
            succeeded: bool,
            error: str | None,
            __: float,
        ) -> None:
            results.append((succeeded, error))
            if succeeded:
                completed.set()

        session = DeviceSession(
            "AA:BB:CC:DD:EE:01",
            on_frame=ignore_frame,
            on_command_result=on_result,
            reconnect_delay=0.0,
        )
        await session.send_command(CommandEvent(session.address, "enter", "ENTRY", cmd_id="c1"))
        worker = asyncio.create_task(session._command_worker(FlakyClient()))
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        session._stop_event.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        self.assertFalse(results[0][0])
        self.assertTrue(results[-1][0])


if __name__ == "__main__":
    unittest.main()
