from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence

from slimhub.config import AppPaths
from slimhub.protocol.nus import DEFAULT_DEVICE_NAME
from slimhub.cli.client import send_request_sync


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slimhub", description="SLIMHUB v2 CLI")
    parser.add_argument("--base-dir", help="Runtime base directory. Default: current directory.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    config_parser = subparsers.add_parser("config", help="Manage local device config.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    config_set = config_subparsers.add_parser("set", help="Set device config field.")
    config_set.add_argument("address")
    config_set.add_argument("field", choices=("name", "location"))
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

    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = AppPaths.from_base(args.base_dir)
    _setup_logging(args.debug)

    try:
        if args.command == "run":
            from slimhub.daemon import SlimHubDaemon

            daemon = SlimHubDaemon(
                paths=paths,
                device_name=args.name,
                scan_timeout=args.scan_timeout,
                scan_interval=args.scan_interval,
                reconnect_delay=args.reconnect_delay,
            )
            asyncio.run(daemon.run(address=args.address, scan=not args.no_scan))
            return 0

        data = _send(paths, args)
        _print_result(args, data)
        return 0
    except FileNotFoundError:
        print(f"slimhub daemon is not running at {paths.socket_path}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _send(paths: AppPaths, args: argparse.Namespace) -> object:
    if args.command == "stop":
        return send_request_sync(paths, "stop")
    if args.command == "devices":
        return send_request_sync(paths, "devices")
    if args.command == "connect":
        return send_request_sync(paths, "connect", {"address": args.address})
    if args.command == "config" and args.config_command == "set":
        return send_request_sync(
            paths,
            "config.set",
            {"address": args.address, "field": args.field, "value": args.value},
        )
    if args.command == "raw" and args.raw_command == "tail":
        return send_request_sync(
            paths,
            "raw.tail",
            {"address": args.address, "lines": args.lines},
        )
    if args.command == "unitspace" and args.unitspace_command == "status":
        return send_request_sync(paths, "unitspace.status")
    raise RuntimeError("unhandled CLI command")


def _print_result(args: argparse.Namespace, data: object) -> None:
    if args.command == "devices":
        _print_devices(data)
        return
    if args.command == "raw":
        for line in (data or {}).get("lines", []):
            print(line)
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_devices(data: object) -> None:
    devices = data if isinstance(data, list) else []
    if not devices:
        print("No devices")
        return
    print(
        f"{'Address':<20} {'Packet MAC':<20} {'Connected':<10} "
        f"{'Waiting':<8} {'Location':<14} {'Name':<20} Error"
    )
    for item in devices:
        print(
            f"{str(item.get('address', '')):<20} "
            f"{str(item.get('packet_address') or ''):<20} "
            f"{str(item.get('connected', False)):<10} "
            f"{str(item.get('waiting_for_advertisement', False)):<8} "
            f"{str(item.get('location', 'undefined')):<14} "
            f"{str(item.get('configured_name') or item.get('name') or ''):<20} "
            f"{str(item.get('last_error') or '')}"
        )


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run_cli(argv))


if __name__ == "__main__":
    main()
