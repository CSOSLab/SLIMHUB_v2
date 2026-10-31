"""Hub-side hazards for an unattended multi-week run (Node demo-stability compat).

Each test pins one failure that used to silently end a loop, writer or session
task until the daemon was restarted.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from slimhub.config import AppPaths
from slimhub.events import StructuredEvent
from slimhub.integrations.legacy_inout import LegacyInOutTracker
from slimhub.logging.legacy_report import LegacyReportWriter, legacy_inout_report
from slimhub.logging.raw_logger import RawDataLogger
from slimhub.protocol.nus import FrameAssembler, RawDataPacket, build_frame, parse_frame

from tests.test_daemon import SlimHubDaemon

# test_device_session installs fake bleak modules before importing the session.
from tests import test_device_session  # noqa: F401
from slimhub.ble.device_session import DeviceSession

ADDRESS = "90:E5:B1:D1:23:16"


async def _settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def structured(kind: str) -> StructuredEvent:
    return StructuredEvent(timestamp=1.0, kind=kind, mac=ADDRESS, data={})


class WriterSurvivalTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_logger_task_survives_a_non_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = RawDataLogger(AppPaths.from_base(tmpdir))
            calls: list[str] = []

            async def flaky(event: StructuredEvent) -> None:
                calls.append(event.kind)
                if event.kind == "bad":
                    raise TypeError("unserialisable")

            logger.write_structured = flaky  # type: ignore[method-assign]
            await logger.start()
            await logger.log_structured(structured("bad"))
            await logger.log_structured(structured("good"))
            await asyncio.wait_for(logger._queue.join(), 2)
            task_alive = not logger._task.done()
            await logger.stop()
        self.assertEqual(calls, ["bad", "good"])
        self.assertTrue(task_alive)
        self.assertEqual(logger.health["write_errors"], 1)

    async def test_legacy_writer_task_survives_a_non_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = LegacyReportWriter(AppPaths.from_base(tmpdir))
            written: list[str] = []

            def flaky(item: object) -> None:
                if not written and not getattr(flaky, "failed", False):
                    flaky.failed = True  # type: ignore[attr-defined]
                    raise ValueError("bad timestamp")
                written.append("ok")

            writer._write_item = flaky  # type: ignore[method-assign]
            await writer.start()
            await writer.log(
                legacy_inout_report(ADDRESS, 10, "2026-10-31 10:00:00"),
                timestamp=1_800_000_000.0,
                location="LIVING",
            )
            await writer.log(
                legacy_inout_report(ADDRESS, 20, "2026-10-31 10:00:01"),
                timestamp=1_800_000_001.0,
                location="LIVING",
            )
            await asyncio.wait_for(writer._queue.join(), 2)
            task_alive = not writer._task.done()
            await writer.stop()
        self.assertEqual(written, ["ok"])
        self.assertTrue(task_alive)
        self.assertEqual(writer.health["write_errors"], 1)


class LegacyInOutFailureTests(unittest.IsolatedAsyncioTestCase):
    def test_state_save_failure_still_returns_rows_and_keeps_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = LegacyInOutTracker(AppPaths.from_base(tmpdir))
            with patch(
                "slimhub.integrations.legacy_inout._write_json_atomic",
                side_effect=OSError("disk full"),
            ), self.assertLogs(level="ERROR"):
                rows = tracker.enter("2026-10-31 10:00:00", "LIVING", ADDRESS)
            self.assertEqual([(r.room, r.direction) for r in rows], [("LIVING", 10)])
            self.assertEqual(tracker.state["active_room"], "LIVING")
            # The token advanced, so a repeat is a no-op instead of a duplicate ENTER.
            self.assertEqual(tracker.enter("2026-10-31 10:00:05", "LIVING", ADDRESS), [])

    async def test_daemon_legacy_inout_failure_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            packet = RawDataPacket(
                flag_human_presence=1, detected=10, flag_env=0, temperature_c=0.0,
                humidity=0, iaq=0, eco2=0, bvoc=0, accuracy=0, flag_sound=0,
                sound=[0] * 16, is_pir_human_detection_event=True,
            )
            with patch.object(daemon.legacy_inout, "enter", side_effect=OSError("disk full")), \
                    self.assertLogs(level="ERROR"):
                await daemon._log_legacy_inout(ADDRESS, packet, "LIVING", 1_800_000_000.0)


class DaemonLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_occupancy_timeout_loop_survives_a_failing_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            ticks = 0

            def expire(_: float) -> list:
                nonlocal ticks
                ticks += 1
                if ticks == 1:
                    raise RuntimeError("bad tick")
                return []

            async def wait_or_stop(_: float) -> None:
                if ticks >= 3:
                    daemon.stop_event.set()
                await asyncio.sleep(0)

            daemon.dean_contract.expire_confirmations = expire  # type: ignore[method-assign]
            daemon._wait_or_stop = wait_or_stop  # type: ignore[method-assign]
            with self.assertLogs(level="ERROR"):
                await asyncio.wait_for(daemon._occupancy_timeout_loop(), 2)
        self.assertGreaterEqual(ticks, 3)


class TruncatedFrameResyncTests(unittest.TestCase):
    """Node now terminates a link when a frame fails midway and re-sends it whole."""

    def test_frame_after_a_truncated_one_is_recovered(self) -> None:
        assembler = FrameAssembler()
        truncated = build_frame(ADDRESS, "REPORT", b"x" * 120)[:60]
        whole = build_frame(ADDRESS, "REPORT", b"after-truncation")
        frames = assembler.push(truncated)
        frames += assembler.push(whole)
        frames += assembler.push(build_frame(ADDRESS, "REPORT", b"third"))
        self.assertIn(whole, frames)
        self.assertEqual(len(frames), 2)

    def test_truncated_tail_is_discarded_on_clear_and_next_link_starts_clean(self) -> None:
        assembler = FrameAssembler()
        self.assertEqual(assembler.push(build_frame(ADDRESS, "REPORT", b"y" * 100)[:40]), [])
        with self.assertLogs(level="WARNING"):
            assembler.clear()
        whole = build_frame(ADDRESS, "REPORT", b"fresh")
        self.assertEqual(assembler.push(whole), [whole])


class RecordingFrames:
    def __init__(self) -> None:
        self.macs: list[str] = []

    async def __call__(self, _: str, frame: object) -> None:
        self.macs.append(frame.packet_type)  # type: ignore[attr-defined]


class SessionHazardTests(unittest.IsolatedAsyncioTestCase):
    async def test_dead_session_task_restarts_on_a_fresh_advertisement(self) -> None:
        session = DeviceSession(ADDRESS, on_frame=RecordingFrames())

        async def finished() -> None:
            return None

        session._task = asyncio.create_task(finished())
        await session._task
        with patch.object(session, "start") as start:
            session.update_target(ADDRESS)
        start.assert_called_once()

    async def test_running_session_task_is_not_restarted_by_an_advertisement(self) -> None:
        session = DeviceSession(ADDRESS, on_frame=RecordingFrames())
        session._task = asyncio.create_task(asyncio.sleep(10))
        try:
            with patch.object(session, "start") as start:
                session.update_target(ADDRESS)
            start.assert_not_called()
        finally:
            session._task.cancel()
            await asyncio.gather(session._task, return_exceptions=True)

    async def test_failing_disconnect_handler_does_not_end_the_session(self) -> None:
        attempts: list[int] = []

        class QuickDropClient:
            mtu_size = 517
            is_connected = True

            def __init__(self, _: object, disconnected_callback: object = None, **__: object) -> None:
                self._callback = disconnected_callback

            async def connect(self) -> None:
                attempts.append(1)

            async def start_notify(self, *_: object) -> None:
                asyncio.get_running_loop().call_later(0.001, self._callback, self)

            async def stop_notify(self, *_: object) -> None:
                return None

            async def disconnect(self) -> None:
                return None

        async def on_state(_: str, connected: bool, __: float) -> None:
            if not connected:
                raise RuntimeError("handler bug")

        session = DeviceSession(
            ADDRESS, on_frame=RecordingFrames(), on_connection_state=on_state,
            reconnect_delay=0.0,
        )
        with patch("slimhub.ble.device_session.BleakClient", QuickDropClient), \
                self.assertLogs(level="ERROR"):
            session.start()
            await asyncio.sleep(0.05)
            alive = not session._task.done()
            await session.stop()
        self.assertTrue(alive)
        self.assertGreaterEqual(len(attempts), 2)

    async def test_unexpected_parse_error_keeps_the_following_frames(self) -> None:
        recorder = RecordingFrames()
        session = DeviceSession(ADDRESS, on_frame=recorder)
        handler = session._build_notify_handler(FrameAssembler())
        first = build_frame(ADDRESS, "REPORT", b"first")
        second = build_frame(ADDRESS, "REPORT", b"second")
        real = parse_frame
        seen = 0

        def flaky(data: bytes):
            nonlocal seen
            seen += 1
            if seen == 1:
                raise KeyError("decoder bug")
            return real(data)

        with patch("slimhub.ble.device_session.parse_frame", flaky), \
                self.assertLogs(level="ERROR"):
            handler(None, bytearray(first + second))
            await _settle()
        self.assertEqual(recorder.macs, ["REPORT"])

    async def test_hung_frame_handler_is_cancelled_instead_of_blocking_reconnect(self) -> None:
        session = DeviceSession(ADDRESS, on_frame=RecordingFrames())

        async def hang() -> None:
            await asyncio.sleep(60)

        task = asyncio.create_task(hang())
        session._inflight_frame_tasks.add(task)
        with patch("slimhub.ble.device_session.FRAME_DRAIN_TIMEOUT_SECONDS", 0.01), \
                self.assertLogs(level="ERROR"):
            await asyncio.wait_for(session._drain_inflight_frames(), 2)
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
