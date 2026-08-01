from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from slimhub.events import CommandEvent
from slimhub.protocol.nus import FrameAssembler, build_frame


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

from slimhub.ble.device_session import BleakError, DeviceSession


async def ignore_frame(_: str, __: object) -> None:
    return None


class FlakyClient:
    def __init__(self) -> None:
        self.attempts = 0

    async def write_gatt_char(self, *_: object, **__: object) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("link lost")


class ConnectFailureClient:
    is_connected = False

    def __init__(self, *_: object, **__: object) -> None:
        pass

    async def connect(self) -> None:
        raise BleakError("failed to discover services, device disconnected")

    async def disconnect(self) -> None:
        return None


class DeviceSessionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_ble_failure_is_concise_and_not_a_false_disconnect(self) -> None:
        states: list[bool] = []

        async def on_state(_: str, connected: bool, __: float) -> None:
            states.append(connected)

        logger = MagicMock()
        session = DeviceSession(
            "AA:BB:CC:DD:EE:01",
            on_frame=ignore_frame,
            on_connection_state=on_state,
            logger=logger,
        )
        with patch("slimhub.ble.device_session.BleakClient", ConnectFailureClient):
            session.start()
            await asyncio.sleep(0.01)
            await session.stop()

        self.assertEqual(states, [])
        logger.warning.assert_called_once()
        logger.exception.assert_not_called()

    async def test_inout_sync_attempts_keep_distinct_request_ids(self) -> None:
        session = DeviceSession("AA:BB:CC:DD:EE:01", on_frame=ignore_frame)

        await session.send_command(
            CommandEvent(
                session.address,
                "inout_sync,bid=a1,state=in,rid=1",
                "TOILET",
            )
        )
        await session.send_command(
            CommandEvent(
                session.address,
                "inout_sync,bid=a1,state=out,rid=2",
                "TOILET",
            )
        )

        self.assertEqual(session.status()["queued_commands"], 2)

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
        await session.send_command(
            CommandEvent(session.address, "node_status", "TOILET", cmd_id="c1")
        )
        worker = asyncio.create_task(session._command_worker(FlakyClient()))
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        session._stop_event.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        self.assertFalse(results[0][0])
        self.assertTrue(results[-1][0])

    async def test_gatt_write_failure_requests_reconnect_and_preserves_command(self) -> None:
        session = DeviceSession(
            "AA:BB:CC:DD:EE:01",
            on_frame=ignore_frame,
            reconnect_delay=0.0,
        )
        await session.send_command(CommandEvent(session.address, "sound_stop", "TOILET"))
        reconnect_requested = asyncio.Event()

        await session._command_worker(FlakyClient(), reconnect_requested)

        self.assertTrue(reconnect_requested.is_set())
        self.assertEqual(session.status()["queued_commands"], 1)

    async def test_reconnect_wait_has_timer_fallback_without_scanner_update(self) -> None:
        session = DeviceSession(
            "AA:BB:CC:DD:EE:01",
            on_frame=ignore_frame,
            reconnect_delay=0.001,
        )
        session._target_updated_event.clear()

        await asyncio.wait_for(session._wait_for_reconnect(), timeout=0.1)

    async def test_sound_commands_remain_ordered_in_serial_writer_queue(self) -> None:
        session = DeviceSession("AA:BB:CC:DD:EE:01", on_frame=ignore_frame)

        await session.send_command(
            CommandEvent(
                session.address,
                "sound_start,label=pee,thr=800,max=60,silence=5",
                "TOILET",
            )
        )
        await session.send_command(
            CommandEvent(session.address, "sound_stop", "TOILET")
        )

        self.assertEqual(session.status()["queued_commands"], 2)
        first_key = await session._command_queue.get()
        second_key = await session._command_queue.get()
        self.assertEqual(session._pending_commands[first_key].command.split(",")[0], "sound_start")
        self.assertEqual(session._pending_commands[second_key].command, "sound_stop")

    async def test_notification_frames_are_dispatched_in_wire_order(self) -> None:
        handled: list[str] = []

        async def on_frame(_: str, frame: object) -> None:
            message = frame.parsed.message
            if message == "first":
                await asyncio.sleep(0.01)
            handled.append(message)

        session = DeviceSession("AA:BB:CC:DD:EE:01", on_frame=on_frame)
        notify = session._build_notify_handler(FrameAssembler())
        stream = build_frame(session.address, "ALERT", b"first") + build_frame(
            session.address,
            "ALERT",
            b"second",
        )

        notify(None, bytearray(stream))
        await asyncio.gather(*list(session._inflight_frame_tasks))

        self.assertEqual(handled, ["first", "second"])

    async def test_source_mac_mismatch_is_rejected_before_payload_parse(self) -> None:
        handled: list[object] = []

        async def on_frame(_: str, frame: object) -> None:
            handled.append(frame)

        logger = MagicMock()
        session = DeviceSession(
            "AA:BB:CC:DD:EE:01",
            on_frame=on_frame,
            logger=logger,
        )
        notify = session._build_notify_handler(FrameAssembler())
        mismatched = build_frame(
            "AA:BB:CC:DD:EE:02",
            "RAWDATA",
            b"\x00" * 33,
        )

        notify(None, bytearray(mismatched))
        await asyncio.sleep(0)

        self.assertEqual(handled, [])
        logger.error.assert_called_once()
        self.assertIn("source MAC mismatch", logger.error.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
