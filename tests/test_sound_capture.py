from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.events import ReportEvent
from slimhub.protocol.nus import (
    ReportPacket,
    build_sound_auto_command,
    build_sound_background_command,
    build_sound_start_command,
)
from slimhub.sound_capture import SoundCaptureStore


MAC = "AA:BB:CC:DD:EE:FF"
CID = "00ab12cd"


def report_event(timestamp: float, event: str, **fields: object) -> ReportEvent:
    values = {
        "src": "SOUND",
        "event": event,
        "cid": CID,
        **{key: str(value) for key, value in fields.items()},
    }
    message = ",".join(f"{key}={value}" for key, value in values.items())
    return ReportEvent(
        timestamp=timestamp,
        mac=MAC,
        source_address=MAC,
        location="TOILET",
        packet=ReportPacket(message=message, fields=values),
        payload=message.encode(),
        connected=True,
    )


class SoundCaptureStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_waited_capture_completes_from_node_sd_reports(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_start_command("pee", max_seconds=60),
            100.0,
        )
        self.assertIsNotNone(request_id)
        waiter = asyncio.create_task(
            store.wait_for_request(request_id, terminal=True, timeout=1.0)
        )

        store.handle_report(report_event(101.0, "CAPTURE_ARMED", label="pee"))
        store.handle_report(report_event(102.0, "CAPTURE_START", label="pee"))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        store.handle_report(
            report_event(
                103.0,
                "CAPTURE_DONE",
                label="pee",
                reason="max_duration",
                complete=1,
                samples=16000,
                blocks=32,
                segment_samples=16000,
                segment_blocks=32,
                queue_drop=0,
                ble_drop=0,
            )
        )

        result = await waiter
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["session"]["storage"], "node_sd")
        self.assertEqual(result["session"]["samples"], 16000)

    async def test_no_wait_returns_only_after_armed_with_cid(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_background_command(max_seconds=10),
            100.0,
        )
        waiter = asyncio.create_task(
            store.wait_for_request(request_id, terminal=False, timeout=1.0)
        )
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        store.handle_report(
            report_event(101.0, "CAPTURE_ARMED", label="background")
        )

        result = await waiter
        self.assertEqual(result["status"], "armed")
        self.assertEqual(result["session"]["cid"], CID)

    async def test_no_wait_ignores_out_of_order_start_until_armed(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_background_command(max_seconds=10),
            100.0,
        )
        waiter = asyncio.create_task(
            store.wait_for_request(request_id, terminal=False, timeout=1.0)
        )

        store.handle_report(
            report_event(101.0, "CAPTURE_START", label="background")
        )
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        store.handle_report(
            report_event(102.0, "CAPTURE_ARMED", label="background")
        )
        result = await waiter
        self.assertEqual(result["status"], "armed")
        self.assertEqual(result["session"]["cid"], CID)

    async def test_incomplete_cancelled_and_capture_error_are_failures(self) -> None:
        for event, complete in (
            ("CAPTURE_DONE", 0),
            ("CAPTURE_CANCELLED", 0),
            ("CAPTURE_ERROR", 0),
        ):
            with self.subTest(event=event):
                store = SoundCaptureStore()
                request_id = store.register_command(
                    MAC,
                    build_sound_background_command(max_seconds=10),
                    100.0,
                )
                store.handle_report(
                    report_event(101.0, "CAPTURE_ARMED", label="background")
                )
                store.handle_report(
                    report_event(
                        102.0,
                        event,
                        label="background",
                        reason="sd_write_error",
                        complete=complete,
                    )
                )

                result = await store.wait_for_request(
                    request_id,
                    terminal=True,
                    timeout=0.1,
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["exit_code"], 1)

    async def test_low_energy_terminal_report_completes_capture(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_background_command(max_seconds=300),
            100.0,
        )
        store.handle_report(report_event(101.0, "CAPTURE_ARMED", label="background"))
        store.handle_report(
            report_event(
                102.0,
                "CAPTURE_DONE",
                label="background",
                reason="low_energy",
                complete=1,
            )
        )

        result = await store.wait_for_request(request_id, terminal=True, timeout=0.1)
        self.assertTrue(result["success"])
        self.assertEqual(result["reason"], "low_energy")

    async def test_storage_error_is_diagnostic_until_done(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_background_command(max_seconds=10),
            100.0,
        )
        store.handle_report(report_event(101.0, "CAPTURE_ARMED", label="background"))
        store.handle_report(
            report_event(
                102.0,
                "CAPTURE_STORAGE_ERROR",
                label="background",
                reason="sd_retry",
            )
        )
        waiter = asyncio.create_task(
            store.wait_for_request(request_id, terminal=True, timeout=1.0)
        )
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        store.handle_report(
            report_event(
                103.0,
                "CAPTURE_DONE",
                label="background",
                reason="sd_write_error",
                complete=0,
            )
        )

        result = await waiter
        self.assertEqual(
            result["session"]["storage_errors"][0]["reason"],
            "sd_retry",
        )

    async def test_disconnect_does_not_finish_waiter_and_cached_done_does(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_background_command(max_seconds=10),
            100.0,
        )
        store.handle_report(report_event(101.0, "CAPTURE_ARMED", label="background"))
        waiter = asyncio.create_task(
            store.wait_for_request(request_id, terminal=True, timeout=1.0)
        )
        store.handle_disconnect(MAC, 102.0)
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        store.handle_report(
            report_event(
                103.0,
                "CAPTURE_DONE",
                label="background",
                reason="max_duration",
                complete=1,
            )
        )
        result = await waiter
        self.assertTrue(result["success"])

    async def test_duplicate_terminal_report_is_idempotent(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_background_command(max_seconds=10),
            100.0,
        )
        store.handle_report(report_event(101.0, "CAPTURE_ARMED", label="background"))
        store.handle_report(
            report_event(
                102.0,
                "CAPTURE_DONE",
                label="background",
                reason="max_duration",
                complete=1,
            )
        )
        store.handle_report(
            report_event(
                103.0,
                "CAPTURE_DONE",
                label="background",
                reason="corrupt_duplicate",
                complete=0,
            )
        )

        result = await store.wait_for_request(request_id, terminal=True, timeout=0.1)
        self.assertTrue(result["success"])
        self.assertEqual(result["reason"], "max_duration")

    async def test_late_nonterminal_report_cannot_reopen_completed_session(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_background_command(max_seconds=10),
            100.0,
        )
        store.handle_report(report_event(101.0, "CAPTURE_ARMED", label="background"))
        store.handle_report(
            report_event(
                102.0,
                "CAPTURE_DONE",
                label="background",
                reason="max_duration",
                complete=1,
            )
        )
        store.handle_report(
            report_event(103.0, "CAPTURE_START", label="background")
        )

        result = await store.wait_for_request(request_id, terminal=True, timeout=0.1)
        self.assertTrue(result["success"])
        self.assertEqual(result["session"]["state"], "DONE")

    async def test_cached_terminal_does_not_bind_a_new_unarmed_request(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_background_command(max_seconds=10),
            100.0,
        )
        store.handle_report(
            report_event(
                101.0,
                "CAPTURE_DONE",
                label="background",
                reason="old_cached_terminal",
                complete=1,
            )
        )

        result = await store.wait_for_request(
            request_id,
            terminal=True,
            timeout=0.001,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "wait_timeout")
        self.assertIsNone(result["session"]["cid"])

    async def test_wait_timeout_does_not_create_central_sound_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            store = SoundCaptureStore(paths)
            request_id = store.register_command(
                MAC,
                build_sound_start_command("pee"),
                100.0,
            )

            result = await store.wait_for_request(
                request_id,
                terminal=True,
                timeout=0.001,
            )

            self.assertEqual(result["reason"], "wait_timeout")
            self.assertFalse((Path(tmpdir) / "data" / "sound").exists())

    async def test_manual_stop_waits_for_terminal_report(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_background_command(max_seconds=300),
            100.0,
        )
        store.handle_report(report_event(101.0, "CAPTURE_ARMED", label="background"))
        await store.wait_for_request(request_id, terminal=False, timeout=0.1)
        revision = store.status_revision(MAC)
        store.register_command(MAC, "sound_stop", 102.0)
        waiter = asyncio.create_task(
            store.wait_for_terminal_after(MAC, after=revision, timeout=1.0)
        )
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        store.handle_report(
            report_event(
                103.0,
                "CAPTURE_DONE",
                label="background",
                reason="command_stop",
                complete=1,
            )
        )

        result = await waiter
        self.assertTrue(result["success"])
        self.assertEqual(result["reason"], "command_stop")

    async def test_automatic_capture_uses_armed_values_and_capture_complete(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_auto_command(),
            100.0,
        )
        store.handle_report(
            report_event(
                101.0,
                "CAPTURE_ARMED",
                label="automatic",
                mode="automatic",
                threshold_rms=912,
                open_db=57,
                close_db=52,
                max_ms=300000,
                silence_ms=20000,
            )
        )
        store.handle_report(
            report_event(
                102.0,
                "CAPTURE_COMPLETE",
                label="automatic",
                reason="silence",
            )
        )

        result = await store.wait_for_request(
            request_id,
            terminal=True,
            timeout=0.1,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["session"]["mode"], "automatic")
        self.assertEqual(result["session"]["threshold_rms"], 912)
        self.assertEqual(result["session"]["open_db"], 57)
        self.assertEqual(result["session"]["close_db"], 52)
        self.assertEqual(result["session"]["max_ms"], 300000)
        self.assertEqual(result["session"]["silence_ms"], 20000)

    async def test_command_error_must_match_pending_sound_command(self) -> None:
        store = SoundCaptureStore()
        request_id = store.register_command(
            MAC,
            build_sound_auto_command(),
            100.0,
        )

        store.handle_report(
            report_event(
                101.0,
                "COMMAND_ERROR",
                command="sound_start",
                reason="old_error",
            )
        )
        store.handle_report(
            report_event(
                102.0,
                "CAPTURE_ARMED",
                label="automatic",
                mode="automatic",
            )
        )

        result = await store.wait_for_request(
            request_id,
            terminal=False,
            timeout=0.1,
        )
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
