from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from slimhub.cli.client import send_request_sync
from slimhub.config import AppPaths, HubConfigStore
from slimhub.integrations.database import ReportDatabaseUpdater
from slimhub.protocol.nus import (
    DEFAULT_DEVICE_NAME,
    MAX_RECORD_SECONDS,
    MIN_RECORD_SECONDS,
    RECORD_STOP_COMMAND,
    VALID_COMMANDS,
    build_record_command,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slimhub-v2", description="SLIMHUB v2 CLI")
    parser.add_argument("--base-dir", help="Runtime base directory. Default: current directory.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    parser.add_argument("--background", action="store_true", help="Run daemon in the background.")

    parser.add_argument("-r", "--run", dest="run_flag", action="store_true", help="Run slimhub client.")
    parser.add_argument("-c", "--config", dest="legacy_config", nargs=3, metavar=("address", "target", "data"))
    parser.add_argument(
        "-s",
        "--service",
        dest="legacy_service",
        nargs=4,
        metavar=("address", "enable/disable", "service", "characteristic"),
    )
    parser.add_argument("-f", "--feature", dest="legacy_feature", nargs=2, metavar=("address", "start/stop"))
    parser.add_argument("-a", "--apply", dest="apply_flag", action="store_true", help="Apply config file.")
    parser.add_argument("-l", "--list", dest="list_flag", action="store_true", help="List registered devices.")
    parser.add_argument("-q", "--quit", dest="quit_flag", action="store_true", help="Quit slimhub client.")
    parser.add_argument("--hubconfig", nargs=2, metavar=("key", "value"), help="Update hub configuration.")
    parser.add_argument("--reset", dest="legacy_reset", nargs=1, metavar=("address",))
    parser.add_argument("--model", dest="legacy_model", nargs=2, metavar=("address", "command"))
    parser.add_argument("--file", dest="legacy_file", nargs=3, metavar=("address", "file_path", "save_path"))

    parser.add_argument("--address", help="Optional BLE address to connect immediately when using --run.")
    parser.add_argument("--name", default=DEFAULT_DEVICE_NAME, help="BLE device name to scan.")
    parser.add_argument("--no-scan", action="store_true", help="Disable BLE scan loop.")
    parser.add_argument("--scan-timeout", type=float, default=5.0, help="BLE scan duration in seconds.")
    parser.add_argument("--scan-interval", type=float, default=10.0, help="Delay between BLE scans in seconds.")
    parser.add_argument("--reconnect-delay", type=float, default=3.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0, help="BLE connect timeout in seconds.")
    parser.add_argument("--notify-timeout", type=float, default=5.0, help="NUS notify subscription timeout in seconds.")

    subparsers = parser.add_subparsers(dest="subcommand")

    run_parser = subparsers.add_parser("run", help="Run the SLIMHUB daemon.")
    run_parser.add_argument("--background", action="store_true", help="Run daemon in the background.")
    run_parser.add_argument("--address", help="Optional BLE address to connect immediately.")
    run_parser.add_argument("--name", default=DEFAULT_DEVICE_NAME, help="BLE device name to scan.")
    run_parser.add_argument("--no-scan", action="store_true", help="Disable BLE scan loop.")
    run_parser.add_argument("--scan-timeout", type=float, default=5.0, help="BLE scan duration in seconds.")
    run_parser.add_argument("--scan-interval", type=float, default=10.0, help="Delay between BLE scans in seconds.")
    run_parser.add_argument("--reconnect-delay", type=float, default=3.0)
    run_parser.add_argument("--connect-timeout", type=float, default=10.0, help="BLE connect timeout in seconds.")
    run_parser.add_argument("--notify-timeout", type=float, default=5.0, help="NUS notify subscription timeout in seconds.")

    subparsers.add_parser("stop", help="Stop the running daemon.")
    subparsers.add_parser("devices", help="List known devices.")

    connect_parser = subparsers.add_parser("connect", help="Connect to a BLE address.")
    connect_parser.add_argument("--address", required=True)

    command_parser = subparsers.add_parser("command", help="Manual NUS commands.")
    command_subparsers = command_parser.add_subparsers(
        dest="command_action",
        required=True,
    )
    command_send = command_subparsers.add_parser("send", help="Send a NUS COMMAND.")
    command_send.add_argument("--address", required=True)
    command_send.add_argument(
        "--command",
        dest="nus_command",
        choices=VALID_COMMANDS,
        required=True,
    )
    command_record = command_subparsers.add_parser(
        "record",
        help="Start DEAN Node sound recording.",
    )
    command_record.add_argument("--address", required=True)
    command_record.add_argument("--seconds", type=_record_seconds)
    command_record_stop = command_subparsers.add_parser(
        "record-stop",
        help="Stop DEAN Node sound recording.",
    )
    command_record_stop.add_argument("--address", required=True)

    config_parser = subparsers.add_parser("config", help="Manage local device config.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    config_set = config_subparsers.add_parser("set", help="Set device config field.")
    config_set.add_argument("address")
    config_set.add_argument("field", choices=("type", "name", "location"))
    config_set.add_argument("value")

    raw_parser = subparsers.add_parser("raw", help="Rawdata commands.")
    raw_subparsers = raw_parser.add_subparsers(dest="raw_command", required=True)
    raw_tail = raw_subparsers.add_parser("tail", help="Show recent rawdata lines.")
    raw_tail.add_argument("--address")
    raw_tail.add_argument("--lines", type=int, default=20)

    unitspace_parser = subparsers.add_parser("unitspace", help="Unitspace commands.")
    unitspace_subparsers = unitspace_parser.add_subparsers(
        dest="unitspace_command",
        required=True,
    )
    unitspace_subparsers.add_parser("status", help="Show unitspace estimator status.")

    power_parser = subparsers.add_parser("power", help="Shadow power-state commands.")
    power_subparsers = power_parser.add_subparsers(
        dest="power_command",
        required=True,
    )
    power_status = power_subparsers.add_parser("status", help="Show shadow power-state status.")
    power_status.add_argument("--address")

    battery_parser = subparsers.add_parser("battery", help="Battery and uSD status commands.")
    battery_subparsers = battery_parser.add_subparsers(
        dest="battery_command",
        required=True,
    )
    battery_status = battery_subparsers.add_parser("status", help="Show latest USD STATUS report.")
    battery_status.add_argument("--address")

    db_parser = subparsers.add_parser(
        "db",
        help="Incrementally ingest REPORT JSONL into MySQL and optionally upload it.",
    )
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
    db_update = db_subparsers.add_parser("update", help="Ingest locally, then upload to remote MySQL.")
    db_update.add_argument("--no-upload", action="store_true", help="Only ingest into local MySQL.")
    db_subparsers.add_parser("ingest", help="Only ingest into local MySQL.")
    db_subparsers.add_parser("upload", help="Only upload previously ingested local rows.")
    db_subparsers.add_parser("status", help="Show cron, local ingest, and remote upload evidence.")

    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not _has_action(args):
        parser.print_help(sys.stderr)
        return 0

    paths = AppPaths.from_base(args.base_dir)
    _setup_logging(args.debug, paths)

    try:
        if _is_run_command(args):
            if args.background:
                return _run_background(argv, paths)

            from slimhub.daemon import SlimHubDaemon

            daemon = SlimHubDaemon(
                paths=paths,
                device_name=args.name,
                scan_timeout=args.scan_timeout,
                scan_interval=args.scan_interval,
                reconnect_delay=args.reconnect_delay,
                connect_timeout=args.connect_timeout,
                notify_timeout=args.notify_timeout,
            )
            print("==== SLIMHUB START ====")
            logging.info("SLIMHUB start")
            asyncio.run(daemon.run(address=args.address, scan=not args.no_scan))
            return 0

        if args.hubconfig:
            data = HubConfigStore(paths).set_field(args.hubconfig[0], args.hubconfig[1]).__dict__
        elif args.subcommand == "db":
            data = _run_database(paths, args)
        else:
            data = _send(paths, args)
        _print_result(args, data)
        return 0
    except FileNotFoundError:
        print("Slimhub server is not running")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _has_action(args: argparse.Namespace) -> bool:
    return any(
        (
            args.run_flag,
            args.legacy_config,
            args.legacy_service,
            args.legacy_feature,
            args.apply_flag,
            args.list_flag,
            args.quit_flag,
            args.hubconfig,
            args.legacy_reset,
            args.legacy_model,
            args.legacy_file,
            args.subcommand,
        )
    )


def _is_run_command(args: argparse.Namespace) -> bool:
    return bool(args.run_flag or args.subcommand == "run")


def _run_background(argv: Sequence[str] | None, paths: AppPaths) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    child_args = [arg for arg in args if arg != "--background"]
    if not _is_module_invocation_available():
        command = [sys.executable, *sys.argv]
        command = [part for part in command if part != "--background"]
    else:
        command = [sys.executable, "-m", "slimhub.cli.app", *child_args]

    out_path = paths.logs_dir / "slimhub-v2.out"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out = out_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        out.close()

    print(f"SLIMHUB background started pid={process.pid}")
    print(f"Output: {out_path}")
    print(f"Runtime log: {paths.logging_path}")
    return 0


def _is_module_invocation_available() -> bool:
    return Path(__file__).name == "app.py"


def _send(paths: AppPaths, args: argparse.Namespace) -> object:
    if args.quit_flag or args.subcommand == "stop":
        return send_request_sync(paths, "stop")
    if args.list_flag or args.subcommand == "devices":
        return send_request_sync(paths, "devices")
    if args.legacy_config:
        address, field, value = args.legacy_config
        return send_request_sync(
            paths,
            "config.set",
            {"address": address, "field": field, "value": value},
        )
    if args.apply_flag:
        return send_request_sync(paths, "config.apply")
    if args.legacy_service:
        address, action, service, characteristic = args.legacy_service
        return send_request_sync(
            paths,
            "service",
            {
                "address": address,
                "action": action,
                "service": service,
                "characteristic": characteristic,
            },
        )
    if args.legacy_reset:
        return send_request_sync(paths, "reset", {"address": args.legacy_reset[0]})
    if args.legacy_model:
        address, command = args.legacy_model
        return send_request_sync(
            paths,
            "model",
            {"address": address, "model_command": command},
        )
    if args.legacy_feature:
        address, command = args.legacy_feature
        return send_request_sync(
            paths,
            "feature",
            {"address": address, "feature_command": command},
        )
    if args.legacy_file:
        address, file_path, save_path = args.legacy_file
        return send_request_sync(
            paths,
            "file",
            {"address": address, "file_path": file_path, "save_path": save_path},
        )
    if args.subcommand == "connect":
        return send_request_sync(paths, "connect", {"address": args.address})
    if args.subcommand == "command" and args.command_action == "send":
        return send_request_sync(
            paths,
            "command.send",
            {"address": args.address, "command": args.nus_command},
        )
    if args.subcommand == "command" and args.command_action == "record":
        return send_request_sync(
            paths,
            "command.send",
            {"address": args.address, "command": build_record_command(args.seconds)},
        )
    if args.subcommand == "command" and args.command_action == "record-stop":
        return send_request_sync(
            paths,
            "command.send",
            {"address": args.address, "command": RECORD_STOP_COMMAND},
        )
    if args.subcommand == "config" and args.config_command == "set":
        return send_request_sync(
            paths,
            "config.set",
            {"address": args.address, "field": args.field, "value": args.value},
        )
    if args.subcommand == "raw" and args.raw_command == "tail":
        return send_request_sync(
            paths,
            "raw.tail",
            {"address": args.address, "lines": args.lines},
        )
    if args.subcommand == "unitspace" and args.unitspace_command == "status":
        return send_request_sync(paths, "unitspace.status")
    if args.subcommand == "power" and args.power_command == "status":
        return send_request_sync(paths, "power.status", {"address": args.address})
    if args.subcommand == "battery" and args.battery_command == "status":
        return send_request_sync(paths, "battery.status", {"address": args.address})
    raise RuntimeError("unhandled CLI command")


def _run_database(paths: AppPaths, args: argparse.Namespace) -> dict[str, object]:
    updater = ReportDatabaseUpdater(paths)
    if args.db_command == "update":
        return updater.update(upload=not args.no_upload)
    if args.db_command == "ingest":
        return updater.ingest()
    if args.db_command == "upload":
        return updater.upload()
    if args.db_command == "status":
        return updater.status()
    raise RuntimeError("unhandled database command")


def _print_result(args: argparse.Namespace, data: object) -> None:
    if args.list_flag or args.subcommand == "devices":
        _print_devices(data)
        return
    if args.subcommand == "raw":
        for line in (data or {}).get("lines", []):
            print(line)
        return
    if args.subcommand == "command":
        _print_command_send(data)
        return
    if args.subcommand == "battery":
        _print_battery_status(data)
        return
    if isinstance(data, str):
        print(data)
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_devices(data: object) -> None:
    devices = data if isinstance(data, list) else []
    if not devices:
        print("No devices")
        return
    print(f"{'Address':<20}{'Type':<15}{'Name':<15}{'Location':<15}{'Connected':<10}")
    for item in devices:
        print(
            f"{str(item.get('address', '')):<20}"
            f"{str(item.get('type') or ''):<15}"
            f"{str(item.get('configured_name') or item.get('name') or ''):<15}"
            f"{str(item.get('location', 'undefined')):<15}"
            f"{str(item.get('connected', False)):<10}"
        )


def _print_command_send(data: object) -> None:
    payload = data if isinstance(data, dict) else {}
    session = payload.get("session")
    session_payload = session if isinstance(session, dict) else {}
    print(
        "NUS write queued: "
        f"command={payload.get('command')} "
        f"address={payload.get('address')} "
        f"session={session_payload.get('address')}"
    )


def _print_battery_status(data: object) -> None:
    if isinstance(data, dict):
        rows = [data] if data else []
    elif isinstance(data, list):
        rows = [item for item in data if isinstance(item, dict)]
    else:
        rows = []

    if not rows:
        print("No battery reports")
        return

    print(
        f"{'Address':<20}{'Location':<12}{'Batt':<8}{'Volt':<8}"
        f"{'mV':<7}{'USB':<5}{'CHG':<5}{'SD':<5}{'File':<18}{'Uptime':<10}"
    )
    for item in rows:
        batt_pct = _format_percent(item.get("batt_pct"))
        batt_v = _format_value(item.get("batt_v"))
        print(
            f"{str(item.get('address', '')):<20}"
            f"{str(item.get('location', 'undefined')):<12}"
            f"{batt_pct:<8}"
            f"{batt_v:<8}"
            f"{str(item.get('batt_mv', '')):<7}"
            f"{str(item.get('usb', '')):<5}"
            f"{str(item.get('chg', '')):<5}"
            f"{str(item.get('sd', '')):<5}"
            f"{str(item.get('file', '')):<18}"
            f"{str(item.get('uptime', '')):<10}"
        )


def _format_percent(value: object) -> str:
    if value in (None, ""):
        return ""
    return f"{value}%"


def _format_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return "" if value is None else str(value)


def _record_seconds(value: str) -> int:
    try:
        seconds = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"seconds must be an integer from {MIN_RECORD_SECONDS} to {MAX_RECORD_SECONDS}"
        ) from exc
    if seconds < MIN_RECORD_SECONDS or seconds > MAX_RECORD_SECONDS:
        raise argparse.ArgumentTypeError(
            f"seconds must be an integer from {MIN_RECORD_SECONDS} to {MAX_RECORD_SECONDS}"
        )
    return seconds


def _setup_logging(debug: bool, paths: AppPaths) -> None:
    paths.ensure()
    logging.basicConfig(
        filename=str(paths.logging_path),
        level=logging.INFO,
        format="%(asctime)s: %(levelname)s: %(message)s",
    )
    logging.getLogger("slimhub").setLevel(logging.INFO)
    for name in (
        "asyncio",
        "bleak",
        "bleak.backends",
        "bleak.backends.bluezdbus",
        "dbus",
        "dbus_fast",
        "dbus_next",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run_cli(argv))


def background_main(argv: Sequence[str] | None = None) -> None:
    """Compatibility entry point matching the legacy slimhub-background alias."""
    args = list(sys.argv[1:] if argv is None else argv)
    raise SystemExit(run_cli(["--run", "--background", *args]))


if __name__ == "__main__":
    main()
