from __future__ import annotations

import io
import tempfile
import unittest
from unittest.mock import patch

from slimhub.cli.app import build_parser, run_cli


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


if __name__ == "__main__":
    unittest.main()
