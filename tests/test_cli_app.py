from __future__ import annotations

import io
import tempfile
import time
import unittest
from argparse import Namespace
from unittest.mock import patch

from slimhub.cli.app import (
    _send_with_default_wait_eta,
    _sound_capture_wait_timeout,
    background_main,
    build_parser,
    run_cli,
)
from slimhub.config import AppPaths


ADDRESS = "AA:BB:CC:DD:EE:FF"


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def sound_response(
    *,
    status: str = "complete",
    success: bool = True,
    reason: str = "max_duration",
) -> dict[str, object]:
    return {
        "request": {"address": ADDRESS},
        "outcome": {
            "status": status,
            "success": success,
            "exit_code": 0 if success else 1,
            "reason": reason,
            "complete": success,
            "session": {
                "node_mac": ADDRESS,
                "location": "KITCHEN",
                "label": "background",
                "cid": "7614f4ce",
                "samples": 160256,
                "blocks": 313,
                "storage": "node_sd",
            },
        },
    }


class CliAppTests(unittest.TestCase):
    def run_command_cli(
        self,
        args: list[str],
        response: object | None = None,
    ) -> tuple[int, object, str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = response or {
                "address": ADDRESS,
                "command": "",
                "session": {"address": ADDRESS},
            }
            with patch("slimhub.cli.app.send_request_sync", return_value=result) as send:
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    status = run_cli(["--base-dir", tmpdir, *args])
            return status, send.call_args, stdout.getvalue()

    def test_record_commands_keep_legacy_payloads(self) -> None:
        for arguments, expected in (
            (["command", "record", "--address", ADDRESS], "record"),
            (
                ["command", "record", "--address", ADDRESS, "--seconds", "15"],
                "record:15",
            ),
            (["command", "record-stop", "--address", ADDRESS], "record_stop"),
        ):
            with self.subTest(expected=expected):
                status, call, _ = self.run_command_cli(arguments)
                self.assertEqual(status, 0)
                self.assertEqual(call.args[1], "command.send")
                self.assertEqual(call.args[2]["command"], expected)

    def test_sound_start_defaults_to_wait_and_never_sends_dest(self) -> None:
        status, call, output = self.run_command_cli(
            [
                "sound",
                "start",
                "--location",
                "TOILET",
                "--label",
                "pee",
                "--threshold-rms",
                "1200",
                "--max-seconds",
                "90",
                "--silence-seconds",
                "5",
            ],
            sound_response(),
        )

        self.assertEqual(status, 0)
        self.assertEqual(call.args[1], "sound.capture")
        self.assertEqual(
            call.args[2],
            {
                "location": "TOILET",
                "command": "sound_start,label=pee,thr=1200,max=90,silence=5",
                "wait": True,
                "timeout": 390,
            },
        )
        self.assertNotIn("dest", str(call.args[2]))
        self.assertIn("SOUND COMPLETE", output)

    def test_sound_background_wait_and_no_wait_payloads(self) -> None:
        waited_status, waited_call, _ = self.run_command_cli(
            [
                "sound",
                "background",
                "--address",
                ADDRESS,
                "--max-seconds",
                "10",
            ],
            sound_response(),
        )
        armed_response = sound_response(status="armed", reason="accepted")
        no_wait_status, no_wait_call, output = self.run_command_cli(
            [
                "sound",
                "background",
                "--address",
                ADDRESS,
                "--max-seconds",
                "300",
                "--no-wait",
            ],
            armed_response,
        )

        self.assertEqual(waited_status, 0)
        self.assertEqual(
            waited_call.args[2]["command"],
            "sound_bg,max=10",
        )
        self.assertTrue(waited_call.args[2]["wait"])
        self.assertEqual(waited_call.args[2]["timeout"], 190)
        self.assertEqual(no_wait_status, 0)
        self.assertFalse(no_wait_call.args[2]["wait"])
        self.assertEqual(no_wait_call.args[2]["timeout"], 180)
        self.assertIn("SOUND ARMED", output)

    def test_sound_catalog_formats_node_authoritative_latest_inference(self) -> None:
        response = [
            {
                "node_mac": ADDRESS,
                "boot_id": "1a2b3c4d",
                "location": "TOILET",
                "model": "0cb81518",
                "class_count": 10,
                "catalog": [
                    {
                        "class_index": 5,
                        "label": "flushing",
                        "class_count": 10,
                        "observations": 1,
                    }
                ],
                "last_inference": {
                    "class_index": 5,
                    "label": "flushing",
                    "semantic": "flushing",
                    "confidence": 0.91,
                    "source": "tflm",
                },
            }
        ]
        status, call, output = self.run_command_cli(
            ["sound", "catalog", "--address", ADDRESS],
            response,
        )

        self.assertEqual(status, 0)
        self.assertEqual(call.args[1], "sound.catalog")
        self.assertEqual(call.args[2], {"address": ADDRESS})
        self.assertIn(
            f"ENTRY {ADDRESS} TOILET model=0cb81518 classes=10",
            output,
        )
        self.assertIn("index=5 label=flushing", output)

    def test_sound_completion_timeout_scales_with_capture_duration(self) -> None:
        self.assertEqual(_sound_capture_wait_timeout("background", 10), 190)
        self.assertEqual(_sound_capture_wait_timeout("background", 600), 900)
        self.assertEqual(_sound_capture_wait_timeout("start", 600), 1020)
        self.assertEqual(_sound_capture_wait_timeout("start", 1800), 2820)

    def test_implicit_wait_shows_eta_only_on_interactive_terminal(self) -> None:
        response = sound_response()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "slimhub.cli.app._send_with_default_wait_eta",
                return_value=response,
            ) as eta_send:
                with patch("sys.stderr", TtyStringIO()):
                    with patch("sys.stdout", new_callable=io.StringIO):
                        status = run_cli(
                            [
                                "--base-dir",
                                tmpdir,
                                "sound",
                                "background",
                                "--address",
                                ADDRESS,
                                "--max-seconds",
                                "10",
                            ]
                        )

        self.assertEqual(status, 0)
        eta_send.assert_called_once()

    def test_explicit_wait_does_not_show_default_eta(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "slimhub.cli.app.send_request_sync",
                return_value=sound_response(),
            ):
                with patch(
                    "slimhub.cli.app._send_with_default_wait_eta",
                ) as eta_send:
                    with patch("sys.stderr", TtyStringIO()):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            status = run_cli(
                                [
                                    "--base-dir",
                                    tmpdir,
                                    "sound",
                                    "background",
                                    "--address",
                                    ADDRESS,
                                    "--wait",
                                ]
                            )

        self.assertEqual(status, 0)
        eta_send.assert_not_called()

    def test_eta_renderer_updates_and_clears_before_final_output(self) -> None:
        args = Namespace(sound_action="background", max_seconds=1)
        stderr = TtyStringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "slimhub.cli.app._send",
                side_effect=lambda *_: (time.sleep(0.03), sound_response())[1],
            ):
                with patch("sys.stderr", stderr):
                    result = _send_with_default_wait_eta(
                        AppPaths.from_base(tmpdir),
                        args,
                    )

        self.assertEqual(result, sound_response())
        self.assertIn("SOUND WAIT", stderr.getvalue())
        self.assertIn("ETA ~", stderr.getvalue())
        self.assertTrue(stderr.getvalue().endswith("\r\033[2K"))

    def test_sound_dest_option_is_rejected(self) -> None:
        parser = build_parser()
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                parser.parse_args(
                    [
                        "sound",
                        "background",
                        "--address",
                        ADDRESS,
                        "--dest",
                        "both",
                    ]
                )
        self.assertEqual(error.exception.code, 2)

    def test_sound_stop_waits_by_default(self) -> None:
        status, call, _ = self.run_command_cli(
            ["sound", "stop", "--address", ADDRESS],
            sound_response(reason="command_stop"),
        )

        self.assertEqual(status, 0)
        self.assertEqual(call.args[1], "sound.stop")
        self.assertEqual(
            call.args[2],
            {"address": ADDRESS, "wait": True, "timeout": 1020},
        )
        parser = build_parser()
        stop_parser = (
            parser._subparsers._group_actions[0]
            .choices["sound"]
            ._subparsers._group_actions[0]
            .choices["stop"]
        )
        self.assertIn("stop command is queued", stop_parser.format_help())

    def test_sound_failure_prints_one_line_and_returns_nonzero(self) -> None:
        response = sound_response(
            status="failed",
            success=False,
            reason="sd_write_error",
        )
        status, _, output = self.run_command_cli(
            ["sound", "background", "--location", "KITCHEN"],
            response,
        )

        self.assertEqual(status, 1)
        self.assertEqual(output.count("\n"), 1)
        self.assertIn("SOUND FAILED", output)
        self.assertIn("reason=sd_write_error", output)
        self.assertIn("complete=0", output)

    def test_sound_returns_nonzero_when_daemon_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "slimhub.cli.app.send_request_sync",
                side_effect=FileNotFoundError,
            ):
                with patch("sys.stdout", new_callable=io.StringIO):
                    status = run_cli(
                        [
                            "--base-dir",
                            tmpdir,
                            "sound",
                            "background",
                            "--address",
                            ADDRESS,
                        ]
                    )

        self.assertEqual(status, 1)

    def test_sound_status_uses_status_api(self) -> None:
        response = {"fresh_report": True, "capture": {"storage": "node_sd"}}
        status, call, output = self.run_command_cli(
            ["sound", "status", "--address", ADDRESS],
            response,
        )

        self.assertEqual(status, 0)
        self.assertEqual(call.args[1], "sound.status")
        self.assertIn('"storage": "node_sd"', output)

    def test_sound_automatic_omits_default_thresholds_and_accepts_pair_override(self) -> None:
        status, call, _ = self.run_command_cli(
            ["sound", "automatic", "--address", ADDRESS, "--no-wait"],
            sound_response(status="armed", reason="accepted"),
        )
        override_status, override_call, _ = self.run_command_cli(
            [
                "sound",
                "automatic",
                "--address",
                ADDRESS,
                "--open-db",
                "60",
                "--close-db",
                "55",
                "--no-wait",
            ],
            sound_response(status="armed", reason="accepted"),
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            call.args[2]["command"],
            "sound_auto,max=300,silence=20",
        )
        self.assertEqual(override_status, 0)
        self.assertEqual(
            override_call.args[2]["command"],
            "sound_auto,max=300,silence=20,open_db=60,close_db=55",
        )

    def test_sound_automatic_rejects_only_one_threshold_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("slimhub.cli.app.send_request_sync") as send:
                with patch("sys.stderr", new_callable=io.StringIO):
                    status = run_cli(
                        [
                            "--base-dir",
                            tmpdir,
                            "sound",
                            "automatic",
                            "--address",
                            ADDRESS,
                            "--open-db",
                            "60",
                        ]
                    )

        self.assertEqual(status, 1)
        send.assert_not_called()

    def test_node_status_and_config_commands_use_node_api(self) -> None:
        status, status_call, _ = self.run_command_cli(
            ["node", "status", "--address", ADDRESS],
            {"cached": {}},
        )
        get_status, get_call, _ = self.run_command_cli(
            ["node", "config", "get", "--address", ADDRESS],
            {"cached": {}},
        )
        set_status, set_call, _ = self.run_command_cli(
            [
                "node",
                "config",
                "set",
                "--address",
                ADDRESS,
                "--node-location",
                "KITCHEN",
                "--profile",
                "kitchen_v1",
            ],
            {"cached": {}},
        )
        reload_status, reload_call, _ = self.run_command_cli(
            ["node", "config", "reload", "--address", ADDRESS],
            {"cached": {}},
        )

        self.assertEqual((status, get_status, set_status, reload_status), (0, 0, 0, 0))
        self.assertEqual(status_call.args[1], "node.status")
        self.assertEqual(get_call.args[1], "node.config.get")
        self.assertEqual(set_call.args[1], "node.config.set")
        self.assertEqual(
            set_call.args[2],
            {
                "address": ADDRESS,
                "node_location": "KITCHEN",
                "profile": "kitchen_v1",
            },
        )
        self.assertEqual(reload_call.args[1], "node.config.reload")

    def test_sound_cli_rejects_invalid_label_and_ranges(self) -> None:
        parser = build_parser()
        invalid_arguments = (
            ["sound", "start", "--address", ADDRESS, "--label", "../pee"],
            [
                "sound",
                "start",
                "--address",
                ADDRESS,
                "--label",
                "pee",
                "--threshold-rms",
                "32768",
            ],
            [
                "sound",
                "background",
                "--address",
                ADDRESS,
                "--max-seconds",
                "1801",
            ],
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as error:
                        parser.parse_args(arguments)
                self.assertEqual(error.exception.code, 2)

    def test_sound_help_describes_node_sd_only(self) -> None:
        parser = build_parser()
        sound = parser._subparsers._group_actions[0].choices["sound"]
        help_text = sound.format_help()

        self.assertIn("stored on the DEAN Node", help_text)
        self.assertIn("audio data is", help_text)
        self.assertIn("never transferred to SLIMHUB_v2", help_text)
        self.assertIn("ACTIVE records to Node uSD", help_text)
        self.assertNotIn("--dest", help_text)
        self.assertNotIn("data/sound", help_text)

    def test_command_and_config_location_targets_remain_supported(self) -> None:
        command_status, command_call, _ = self.run_command_cli(
            ["command", "send", "--location", "TOILET", "--command", "enter"]
        )
        config_status, config_call, _ = self.run_command_cli(
            ["config", "set", "--location", "TOILET", "name", "toilet-node"]
        )

        self.assertEqual(command_status, 0)
        self.assertEqual(
            command_call.args[2],
            {"location": "TOILET", "command": "enter"},
        )
        self.assertEqual(config_status, 0)
        self.assertEqual(
            config_call.args[2],
            {"location": "TOILET", "field": "name", "value": "toilet-node"},
        )

    def test_legacy_help_and_hidden_run_options_remain_supported(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            status = run_cli(["--legacy-help"])
        args = build_parser().parse_args(
            ["--run", "--background", "--scan-timeout", "8"]
        )

        self.assertEqual(status, 0)
        self.assertIn("-r, --run", stdout.getvalue())
        self.assertTrue(args.run_flag)
        self.assertTrue(args.background)
        self.assertEqual(args.scan_timeout, 8.0)

    def test_battery_status_prints_summary(self) -> None:
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
        status, call, output = self.run_command_cli(
            ["battery", "status", "--address", ADDRESS],
            response,
        )

        self.assertEqual(status, 0)
        self.assertEqual(call.args[1], "battery.status")
        self.assertIn("75%", output)
        self.assertIn("3.980", output)

    def test_run_background_starts_detached_process(self) -> None:
        class FakeProcess:
            pid = 12345

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "slimhub.cli.app.subprocess.Popen",
                return_value=FakeProcess(),
            ) as popen:
                with patch("sys.stdout", new_callable=io.StringIO):
                    status = run_cli(
                        [
                            "--base-dir",
                            tmpdir,
                            "--debug",
                            "run",
                            "--background",
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_background_compatibility_entry_point_starts_daemon(self) -> None:
        class FakeProcess:
            pid = 24680

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "slimhub.cli.app.subprocess.Popen",
                return_value=FakeProcess(),
            ) as popen:
                with patch("sys.stdout", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as result:
                        background_main(["--base-dir", tmpdir])

        self.assertEqual(result.exception.code, 0)
        self.assertIn("--run", popen.call_args.args[0])

    def test_debug_logging_keeps_dependency_logs_quiet(self) -> None:
        import logging

        from slimhub.cli.app import _setup_logging

        with tempfile.TemporaryDirectory() as tmpdir:
            for handler in logging.getLogger().handlers[:]:
                logging.getLogger().removeHandler(handler)
                handler.close()
            _setup_logging(True, AppPaths.from_base(tmpdir))

            self.assertEqual(logging.getLogger("slimhub").level, logging.DEBUG)
            self.assertEqual(logging.getLogger("bleak").level, logging.WARNING)
            for handler in logging.getLogger().handlers[:]:
                logging.getLogger().removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
