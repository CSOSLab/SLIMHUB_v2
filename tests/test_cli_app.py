from __future__ import annotations

import io
import tempfile
import unittest
from unittest.mock import patch

from slimhub.cli.app import background_main, build_parser, run_cli
from slimhub.config import AppPaths


ADDRESS = "AA:BB:CC:DD:EE:FF"


class CliAppTests(unittest.TestCase):
    def run_command_cli(self, args: list[str]) -> tuple[int, object]:
        with tempfile.TemporaryDirectory() as tmpdir:
            response = {"address": ADDRESS, "command": "", "session": {"address": ADDRESS}}
            with patch("slimhub.cli.app.send_request_sync", return_value=response) as send:
                with patch("sys.stdout", new_callable=io.StringIO):
                    status = run_cli(["--base-dir", tmpdir, *args])
            return status, send.call_args

    def test_record_command_sends_record_payload(self) -> None:
        status, call_args = self.run_command_cli(
            ["command", "record", "--address", ADDRESS]
        )

        self.assertEqual(status, 0)
        self.assertEqual(call_args.args[1], "command.send")
        self.assertEqual(call_args.args[2], {"address": ADDRESS, "command": "record"})

    def test_record_seconds_command_sends_duration_payload(self) -> None:
        status, call_args = self.run_command_cli(
            ["command", "record", "--address", ADDRESS, "--seconds", "15"]
        )

        self.assertEqual(status, 0)
        self.assertEqual(call_args.args[1], "command.send")
        self.assertEqual(call_args.args[2], {"address": ADDRESS, "command": "record:15"})

    def test_record_stop_command_sends_stop_payload(self) -> None:
        status, call_args = self.run_command_cli(
            ["command", "record-stop", "--address", ADDRESS]
        )

        self.assertEqual(status, 0)
        self.assertEqual(call_args.args[1], "command.send")
        self.assertEqual(
            call_args.args[2],
            {"address": ADDRESS, "command": "record_stop"},
        )

    def test_record_seconds_cli_rejects_invalid_values(self) -> None:
        parser = build_parser()
        for seconds in ("0", "301", "abc"):
            with self.subTest(seconds=seconds):
                with patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as error:
                        parser.parse_args(
                            [
                                "command",
                                "record",
                                "--address",
                                ADDRESS,
                                "--seconds",
                                seconds,
                            ]
                        )
                self.assertEqual(error.exception.code, 2)

    def test_command_send_still_accepts_enter_and_exit(self) -> None:
        parser = build_parser()

        enter = parser.parse_args(["command", "send", "--address", ADDRESS, "--command", "enter"])
        exit_ = parser.parse_args(["command", "send", "--address", ADDRESS, "--command", "exit"])

        self.assertEqual(enter.nus_command, "enter")
        self.assertEqual(exit_.nus_command, "exit")

    def test_battery_status_requests_and_prints_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            response = {
                "address": ADDRESS,
                "location": "ENTRY",
                "batt_pct": 75,
                "batt_v": 3.98,
                "batt_mv": 3980,
                "usb": 0,
                "chg": 1,
                "sd": 0,
                "file": "LOG/001.CSV",
                "uptime": 12345,
            }
            with patch("slimhub.cli.app.send_request_sync", return_value=response) as send:
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    status = run_cli(
                        [
                            "--base-dir",
                            tmpdir,
                            "battery",
                            "status",
                            "--address",
                            ADDRESS,
                        ]
                    )

            self.assertEqual(status, 0)
            self.assertEqual(send.call_args.args[1], "battery.status")
            self.assertEqual(send.call_args.args[2], {"address": ADDRESS})
            output = stdout.getvalue()
            self.assertIn("75%", output)
            self.assertIn("3.980", output)
            self.assertIn("LOG/001.CSV", output)

    def test_run_background_starts_detached_process(self) -> None:
        class FakeProcess:
            pid = 12345

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("slimhub.cli.app.subprocess.Popen", return_value=FakeProcess()) as popen:
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    status = run_cli(
                        [
                            "--base-dir",
                            tmpdir,
                            "--debug",
                            "run",
                            "--background",
                            "--scan-timeout",
                            "8",
                            "--scan-interval",
                            "5",
                        ]
                    )

            self.assertEqual(status, 0)
            command = popen.call_args.args[0]
            self.assertEqual(command[:3], [__import__("sys").executable, "-m", "slimhub.cli.app"])
            self.assertIn("--debug", command)
            self.assertIn("run", command)
            self.assertIn("--scan-timeout", command)
            self.assertNotIn("--background", command)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertIn("pid=12345", stdout.getvalue())

    def test_debug_logging_does_not_enable_noisy_dependency_debug(self) -> None:
        import logging

        from slimhub.cli.app import _setup_logging

        with tempfile.TemporaryDirectory() as tmpdir:
            for handler in logging.getLogger().handlers[:]:
                logging.getLogger().removeHandler(handler)
                handler.close()

            _setup_logging(True, AppPaths.from_base(tmpdir))

            self.assertEqual(logging.getLogger().level, logging.INFO)
            self.assertEqual(logging.getLogger("slimhub").level, logging.INFO)
            self.assertEqual(logging.getLogger("bleak").level, logging.WARNING)
            self.assertEqual(logging.getLogger("dbus_fast").level, logging.WARNING)
            for handler in logging.getLogger().handlers[:]:
                logging.getLogger().removeHandler(handler)
                handler.close()

    def test_db_update_runs_locally_without_daemon_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("slimhub.cli.app.ReportDatabaseUpdater") as updater_type:
                updater_type.return_value.update.return_value = {
                    "ingest": {"records": 0},
                    "upload": {"skipped": True},
                }
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    status = run_cli(["--base-dir", tmpdir, "db", "update", "--no-upload"])

            self.assertEqual(status, 0)
            updater_type.return_value.update.assert_called_once_with(upload=False)
            self.assertIn('"records": 0', stdout.getvalue())

    def test_db_status_runs_locally_without_daemon_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("slimhub.cli.app.ReportDatabaseUpdater") as updater_type:
                updater_type.return_value.status.return_value = {"last_update": None}
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    status = run_cli(["--base-dir", tmpdir, "db", "status"])

            self.assertEqual(status, 0)
            updater_type.return_value.status.assert_called_once_with()
            self.assertIn('"last_update": null', stdout.getvalue())

    def test_background_compatibility_entry_point_starts_daemon(self) -> None:
        class FakeProcess:
            pid = 24680

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("slimhub.cli.app.subprocess.Popen", return_value=FakeProcess()) as popen:
                with patch("sys.stdout", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as result:
                        background_main(["--base-dir", tmpdir])

            self.assertEqual(result.exception.code, 0)
            command = popen.call_args.args[0]
            self.assertIn("--run", command)
            self.assertNotIn("--background", command)


if __name__ == "__main__":
    unittest.main()
