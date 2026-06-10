from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence

from slimhub.cli.client import send_request_sync
from slimhub.config import AppPaths, HubConfigStore
from slimhub.protocol.nus import DEFAULT_DEVICE_NAME, VALID_COMMANDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slimhub-v2", description="SLIMHUB v2 CLI")
    parser.add_argument("--base-dir", help="Runtime base directory. Default: current directory.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")

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
    parser.add_argument("--scan-timeout", type=float, default=5.0)
    parser.add_argument("--scan-interval", type=float, default=10.0)
    parser.add_argument("--reconnect-delay", type=float, default=3.0)

    subparsers = parser.add_subparsers(dest="subcommand")

    run_parser = subparsers.add_parser("run", help="Run the SLIMHUB daemon.")
    run_parser.add_argument("--address", help="Optional BLE address to connect immediately.")
    run_parser.add_argument("--name", default=DEFAULT_DEVICE_NAME, help="BLE device name to scan.")
    run_parser.add_argument("--no-scan", action="store_true", help="Disable BLE scan loop.")
    run_parser.add_argument("--scan-timeout", type=float, default=5.0)
    run_parser.add_argument("--scan-interval", type=float, default=10.0)
    run_parser.add_argument("--reconnect-delay", type=float, default=3.0)

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
            from slimhub.daemon import SlimHubDaemon

            daemon = SlimHubDaemon(
                paths=paths,
                device_name=args.name,
                scan_timeout=args.scan_timeout,
                scan_interval=args.scan_interval,
                reconnect_delay=args.reconnect_delay,
            )
            print("==== SLIMHUB START ====")
            logging.info("SLIMHUB start")
            asyncio.run(daemon.run(address=args.address, scan=not args.no_scan))
            return 0

        if args.hubconfig:
            data = HubConfigStore(paths).set_field(args.hubconfig[0], args.hubconfig[1]).__dict__
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
    raise RuntimeError("unhandled CLI command")


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


def _setup_logging(debug: bool, paths: AppPaths) -> None:
    paths.ensure()
    logging.basicConfig(
        filename=str(paths.logging_path),
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s: %(levelname)s: %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run_cli(argv))


if __name__ == "__main__":
    main()
