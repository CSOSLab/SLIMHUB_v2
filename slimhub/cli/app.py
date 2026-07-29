from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path

from slimhub.cli.client import send_request_sync
from slimhub.config import AppPaths, DeviceConfigStore, HubConfigStore
from slimhub.integrations.database import DataDirectoryDatabaseUpdater
from slimhub.protocol.nus import (
    DEFAULT_DEVICE_NAME,
    MAX_RECORD_SECONDS,
    MAX_SOUND_SECONDS,
    MIN_RECORD_SECONDS,
    RECORD_STOP_COMMAND,
    SOUND_STATUS_COMMAND,
    VALID_COMMANDS,
    build_record_command,
    build_sound_background_command,
    build_sound_auto_command,
    build_sound_start_command,
    validate_sound_label,
)

SOUND_ARM_WAIT_SECONDS = 120
SOUND_FINALIZE_MIN_SECONDS = 180
SOUND_NO_WAIT_ACCEPT_SECONDS = 180
SOUND_STOP_WAIT_SECONDS = SOUND_ARM_WAIT_SECONDS + (MAX_SOUND_SECONDS // 2)
SOUND_INTERRUPT_STOP_WAIT_SECONDS = SOUND_FINALIZE_MIN_SECONDS


class _HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Keep examples readable while showing meaningful option defaults."""

    def _get_help_string(self, action: argparse.Action) -> str:
        help_text = action.help or ""
        if (
            action.option_strings
            and action.default not in (None, False, argparse.SUPPRESS)
            and "%(default)" not in help_text
        ):
            help_text += " (default: %(default)s)"
        return help_text


def _add_target_options(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
    address_help: str = "Target one Node by MAC address.",
    location_help: str = "Target one Node by its unique configured location.",
) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--address", metavar="MAC", help=address_help)
    group.add_argument("--location", metavar="NAME", help=location_help)


def _add_wait_option(
    parser: argparse.ArgumentParser,
    *,
    no_wait_result: str = "CAPTURE_ARMED confirms the cid",
) -> None:
    parser.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Wait for the Node terminal REPORT; "
            f"--no-wait returns after {no_wait_result}."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slimhub-v2",
        description=(
            "Operate the SLIMHUB v2 BLE collector, connected DEAN Node v2 devices,\n"
            "captured data, and the separate database synchronization pipeline.\n\n"
            "The daemon must be running for device, command, sound, and status commands.\n"
            "Database commands run directly and do not require the daemon socket."
        ),
        epilog=(
            "Typical deployment workflow:\n"
            "  slimhub-v2 run --background\n"
            "  slimhub-v2 devices\n"
            "  slimhub-v2 config set --address AA:BB:CC:DD:EE:FF location TOILET\n"
            "  slimhub-v2 sound status --location TOILET\n"
            "  slimhub-v2 db ingest\n"
            "  slimhub-v2 db status\n\n"
            "Help navigation:\n"
            "  slimhub-v2 <command> --help\n"
            "  slimhub-v2 <command> <action> --help\n"
            "  slimhub-v2 --legacy-help\n\n"
            "Global options such as --base-dir and --debug must appear before the command."
        ),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        "--base-dir",
        metavar="PATH",
        help=(
            "Runtime root containing data/, programdata/, logs/, and the daemon socket. "
            "Default: SLIMHUB_HOME or the current directory."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable SLIMHUB debug logs; noisy BLE/DBus dependency logs remain filtered.",
    )
    parser.add_argument(
        "--legacy-help",
        action="store_true",
        help="Show compatibility options retained from SLIMHUB v1.",
    )

    # Keep v1 flat options parseable for installed deployments, but direct new
    # operators to the structured v2 commands listed by the normal help text.
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-r", "--run", dest="run_flag", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "-c",
        "--config",
        dest="legacy_config",
        nargs=3,
        metavar=("address", "target", "data"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-s",
        "--service",
        dest="legacy_service",
        nargs=4,
        metavar=("address", "enable/disable", "service", "characteristic"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-f",
        "--feature",
        dest="legacy_feature",
        nargs=2,
        metavar=("address", "start/stop"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("-a", "--apply", dest="apply_flag", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-l", "--list", dest="list_flag", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-q", "--quit", dest="quit_flag", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hubconfig", nargs=2, metavar=("key", "value"), help=argparse.SUPPRESS)
    parser.add_argument("--reset", dest="legacy_reset", nargs=1, metavar=("address",), help=argparse.SUPPRESS)
    parser.add_argument("--model", dest="legacy_model", nargs=2, metavar=("address", "command"), help=argparse.SUPPRESS)
    parser.add_argument("--file", dest="legacy_file", nargs=3, metavar=("address", "file_path", "save_path"), help=argparse.SUPPRESS)

    parser.add_argument("--address", help=argparse.SUPPRESS)
    parser.add_argument("--name", default=DEFAULT_DEVICE_NAME, help=argparse.SUPPRESS)
    parser.add_argument("--no-scan", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scan-timeout", type=float, default=5.0, help=argparse.SUPPRESS)
    parser.add_argument("--scan-interval", type=float, default=10.0, help=argparse.SUPPRESS)
    parser.add_argument("--reconnect-delay", type=float, default=3.0, help=argparse.SUPPRESS)
    parser.add_argument("--connect-timeout", type=float, default=10.0, help=argparse.SUPPRESS)
    parser.add_argument("--notify-timeout", type=float, default=5.0, help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="subcommand")

    run_parser = subparsers.add_parser(
        "run",
        help="Start the BLE collection daemon (foreground or background).",
        description=(
            "Start the long-running BLE daemon. It scans for DEAN Node v2 devices,\n"
            "parses NUS frames, writes data/display files, and serves the local CLI socket."
        ),
        epilog=(
            "Examples:\n"
            "  slimhub-v2 run\n"
            "  slimhub-v2 run --background\n"
            "  slimhub-v2 run --address AA:BB:CC:DD:EE:FF --no-scan"
        ),
        formatter_class=_HelpFormatter,
    )
    run_parser.add_argument(
        "--background",
        action="store_true",
        help="Detach the daemon and write launcher output to logs/slimhub-v2.out.",
    )
    _add_target_options(
        run_parser,
        required=False,
        address_help="Connect this BLE address immediately in addition to normal scanning.",
        location_help="Resolve this configured location and connect its device immediately.",
    )
    run_parser.add_argument(
        "--name",
        default=DEFAULT_DEVICE_NAME,
        metavar="NAME",
        help="Advertised BLE device name accepted by the scanner.",
    )
    run_parser.add_argument(
        "--no-scan",
        action="store_true",
        help="Disable periodic scanning; normally combine with --address or --location.",
    )
    run_parser.add_argument(
        "--scan-timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Duration of each BLE scan window.",
    )
    run_parser.add_argument(
        "--scan-interval",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="Delay before starting the next BLE scan.",
    )
    run_parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="Delay before retrying a disconnected BLE session.",
    )
    run_parser.add_argument(
        "--connect-timeout",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="Maximum time allowed for one BLE connection attempt.",
    )
    run_parser.add_argument(
        "--notify-timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Maximum time allowed to subscribe to NUS notifications.",
    )

    subparsers.add_parser(
        "stop",
        help="Ask the daemon to shut down cleanly.",
        description="Stop the daemon through its local Unix socket after pending cleanup.",
    )
    subparsers.add_parser(
        "devices",
        help="List configured and currently connected devices.",
        description=(
            "Show device MAC, type, configured name/location, connection state,\n"
            "and aliases known by the running daemon."
        ),
        formatter_class=_HelpFormatter,
    )

    connect_parser = subparsers.add_parser(
        "connect",
        help="Add a device target to the running daemon.",
        description=(
            "Create or resume a daemon-managed BLE session by MAC or unique location.\n"
            "This does not start the daemon; use 'run' first."
        ),
        formatter_class=_HelpFormatter,
    )
    _add_target_options(connect_parser, required=True)

    command_parser = subparsers.add_parser(
        "command",
        help="Send low-level NUS control commands.",
        description=(
            "Send manual COMMAND frames through the daemon's serialized BLE writer.\n"
            "Use 'send' for IN/OUT feedback. 'record' and 'record-stop' are legacy,\n"
            "unlabeled recording commands; use the 'sound' group for new PCM datasets."
        ),
        epilog=(
            "Examples:\n"
            "  slimhub-v2 command send --address AA:BB:CC:DD:EE:FF --command enter\n"
            "  slimhub-v2 command send --location TOILET --command enter\n"
            "  slimhub-v2 command record --location TOILET --seconds 30"
        ),
        formatter_class=_HelpFormatter,
    )
    command_subparsers = command_parser.add_subparsers(
        dest="command_action",
        required=True,
    )
    command_send = command_subparsers.add_parser(
        "send",
        help="Send an enter/exit feedback command.",
        description="Queue a manual IN/OUT feedback COMMAND for one connected Node.",
        formatter_class=_HelpFormatter,
    )
    _add_target_options(command_send, required=True)
    command_send.add_argument(
        "--command",
        dest="nus_command",
        choices=VALID_COMMANDS,
        required=True,
        help="Feedback state to send to the Node.",
    )
    command_record = command_subparsers.add_parser(
        "record",
        help="Start legacy unlabeled Node recording (compatibility).",
        description=(
            "Send legacy record or record:<seconds>. This does not use the labeled\n"
            "Node-uSD capture workflow; prefer 'slimhub-v2 sound start'."
        ),
        formatter_class=_HelpFormatter,
    )
    _add_target_options(command_record, required=True)
    command_record.add_argument(
        "--seconds",
        type=_record_seconds,
        metavar="1..300",
        help="Optional legacy recording duration; omit for plain 'record'.",
    )
    command_record_stop = command_subparsers.add_parser(
        "record-stop",
        help="Stop legacy unlabeled Node recording (compatibility).",
        description="Send the legacy record_stop payload.",
    )
    _add_target_options(command_record_stop, required=True)

    sound_parser = subparsers.add_parser(
        "sound",
        help="Capture labeled PCM audio on the DEAN Node uSD card.",
        description=(
            "Control explicit, labeled 16 kHz mono PCM capture stored on the DEAN Node\n"
            "uSD card. BLE carries commands and completion reports only; audio data is\n"
            "never transferred to SLIMHUB_v2."
        ),
        epilog=(
            "Capture states: ARMED waits for the RMS trigger; ACTIVE records to Node uSD;\n"
            "DONE/CANCELLED returns a final completion report.\n"
            "On an interactive terminal, omitting both wait options shows an in-place ETA;\n"
            "explicit --wait stays quiet until the final result.\n\n"
            "Examples:\n"
            "  slimhub-v2 sound start --location TOILET --label pee\n"
            "  slimhub-v2 sound background --location TOILET --max-seconds 10\n"
            "  slimhub-v2 sound background --location TOILET --no-wait\n"
            "  slimhub-v2 sound status --location TOILET\n"
            "  slimhub-v2 sound catalog --location TOILET\n"
            "  slimhub-v2 sound stop --location TOILET"
        ),
        formatter_class=_HelpFormatter,
    )
    sound_subparsers = sound_parser.add_subparsers(
        dest="sound_action",
        required=True,
    )
    sound_start = sound_subparsers.add_parser(
        "start",
        help="Arm a validated, labeled capture.",
        description=(
            "Arm a capture and wait for block RMS to reach the threshold. Setting\n"
            "--threshold-rms 0 starts without a gate and forces silence seconds to 0."
        ),
        formatter_class=_HelpFormatter,
    )
    _add_target_options(sound_start, required=True)
    sound_start.add_argument(
        "--label",
        required=True,
        type=_sound_label,
        metavar="LABEL",
        help="Dataset label: 1-24 letters, digits, '_' or '-'.",
    )
    sound_start.add_argument(
        "--threshold-rms",
        type=_bounded_integer("threshold RMS", 0, 32767),
        default=800,
        metavar="0..32767",
        help="Raw PCM16 block RMS gate; 0 disables gating.",
    )
    sound_start.add_argument(
        "--max-seconds",
        type=_bounded_integer("max seconds", 1, 1800),
        default=60,
        metavar="1..1800",
        help="Maximum duration after capture becomes ACTIVE.",
    )
    sound_start.add_argument(
        "--silence-seconds",
        type=_bounded_integer("silence seconds", 0, 60),
        default=5,
        metavar="0..60",
        help="Continuous below-threshold time that ends an ACTIVE capture; 0 disables it.",
    )
    _add_wait_option(sound_start)

    sound_background = sound_subparsers.add_parser(
        "background",
        help="Capture the fixed background label without an RMS gate.",
        description=(
            "Capture label=background with threshold=0 and silence=0. The label cannot\n"
            "be overridden, preventing accidental background dataset fragmentation."
        ),
        formatter_class=_HelpFormatter,
    )
    _add_target_options(sound_background, required=True)
    sound_background.add_argument(
        "--max-seconds",
        type=_bounded_integer("max seconds", 1, 1800),
        default=300,
        metavar="1..1800",
        help="Maximum background capture duration.",
    )
    _add_wait_option(sound_background)

    sound_stop = sound_subparsers.add_parser("stop", help="Stop or cancel capture.")
    _add_target_options(sound_stop, required=True)
    _add_wait_option(sound_stop, no_wait_result="the stop command is queued")
    sound_status = sound_subparsers.add_parser("status", help="Request and show capture state.")
    _add_target_options(sound_status, required=True)
    sound_catalog = sound_subparsers.add_parser(
        "catalog",
        help="Show Node-authoritative model labels and the latest inference.",
    )
    _add_target_options(
        sound_catalog,
        required=False,
        address_help="Restrict the catalog to one Node; omit for every observed Node.",
        location_help="Restrict the catalog to one uniquely configured location.",
    )

    sound_automatic = sound_subparsers.add_parser(
        "automatic",
        help="Run automatic threshold capture using the Node's stored defaults.",
        description=(
            "Send sound_auto. By default no dB thresholds are transmitted, so the "
            "Node's stored 57/52 dB settings remain authoritative."
        ),
        formatter_class=_HelpFormatter,
    )
    _add_target_options(sound_automatic, required=True)
    sound_automatic.add_argument(
        "--max-seconds",
        type=_bounded_integer("max seconds", 1, 1800),
        default=300,
        metavar="1..1800",
    )
    sound_automatic.add_argument(
        "--silence-seconds",
        type=_bounded_integer("silence seconds", 0, 60),
        default=20,
        metavar="0..60",
    )
    sound_automatic.add_argument(
        "--open-db",
        type=_bounded_integer("open dB", 0, 120),
        metavar="0..120",
        help="Optional opening threshold; requires --close-db.",
    )
    sound_automatic.add_argument(
        "--close-db",
        type=_bounded_integer("close dB", 0, 120),
        metavar="0..120",
        help="Optional closing threshold; requires --open-db.",
    )
    _add_wait_option(sound_automatic)

    node_parser = subparsers.add_parser(
        "node",
        help="Query and configure DEAN Node v2 runtime state.",
        formatter_class=_HelpFormatter,
    )
    node_subparsers = node_parser.add_subparsers(dest="node_action", required=True)
    node_status = node_subparsers.add_parser("status", help="Request NODE/STATUS.")
    _add_target_options(node_status, required=True)
    node_config = node_subparsers.add_parser("config", help="Manage Node configuration.")
    node_config_subparsers = node_config.add_subparsers(
        dest="node_config_action",
        required=True,
    )
    node_config_get = node_config_subparsers.add_parser("get", help="Request CONFIG/STATUS.")
    _add_target_options(node_config_get, required=True)
    node_config_set = node_config_subparsers.add_parser(
        "set",
        help="Apply a supported location/profile pair while the Node is OUT+IDLE.",
    )
    _add_target_options(node_config_set, required=True)
    node_config_set.add_argument(
        "--node-location",
        required=True,
        choices=("TOILET", "KITCHEN", "LIVING", "BEDROOM"),
    )
    node_config_set.add_argument(
        "--profile",
        required=True,
        choices=("toilet_v1", "kitchen_v1", "living_v1"),
    )
    node_config_reload = node_config_subparsers.add_parser(
        "reload",
        help="Reload Node configuration while the Node is OUT+IDLE.",
    )
    _add_target_options(node_config_reload, required=True)

    config_parser = subparsers.add_parser(
        "config",
        help="Set or apply local device metadata.",
        description=(
            "Manage the MAC-to-name/location/type configuration stored under programdata.\n"
            "'set' writes one field; 'apply' refreshes active daemon sessions."
        ),
        epilog=(
            "Examples:\n"
            "  slimhub-v2 config set --address AA:BB:CC:DD:EE:FF location TOILET\n"
            "  slimhub-v2 config set --location TOILET name toilet-node\n"
            "  slimhub-v2 config apply"
        ),
        formatter_class=_HelpFormatter,
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    config_set = config_subparsers.add_parser(
        "set",
        help="Set one device configuration field.",
        formatter_class=_HelpFormatter,
    )
    config_target = config_set.add_mutually_exclusive_group()
    config_target.add_argument(
        "--address",
        dest="target_address",
        metavar="MAC",
        help="Target a device by MAC address.",
    )
    config_target.add_argument(
        "--location",
        dest="target_location",
        metavar="NAME",
        help="Target a device by its current unique location.",
    )
    config_set.add_argument(
        "legacy_address",
        nargs="?",
        metavar="ADDRESS",
        help="Compatibility positional MAC; prefer --address.",
    )
    config_set.add_argument(
        "field",
        choices=("type", "name", "location"),
        help="Field to update.",
    )
    config_set.add_argument("value", help="New field value, for example TOILET.")
    config_subparsers.add_parser(
        "apply",
        help="Refresh active sessions from saved device configuration.",
        description="Apply saved names and metadata to sessions in the running daemon.",
    )

    raw_parser = subparsers.add_parser(
        "raw",
        help="Inspect recently collected legacy-compatible rawdata.",
        description=(
            "Read collected data files through the daemon. This is a read-only operator\n"
            "view and does not alter database ingest offsets."
        ),
        formatter_class=_HelpFormatter,
    )
    raw_subparsers = raw_parser.add_subparsers(dest="raw_command", required=True)
    raw_tail = raw_subparsers.add_parser(
        "tail",
        help="Show the latest rawdata lines.",
        formatter_class=_HelpFormatter,
    )
    _add_target_options(
        raw_tail,
        required=False,
        address_help="Restrict output to one Node; omit both target options for all devices.",
        location_help="Restrict output to one uniquely configured location.",
    )
    raw_tail.add_argument(
        "--lines",
        type=int,
        default=20,
        metavar="COUNT",
        help="Maximum number of recent lines to return.",
    )

    unitspace_parser = subparsers.add_parser(
        "unitspace",
        help="Inspect the cross-room IN/OUT estimator.",
        description="Show the daemon's current movement-estimator state and evidence.",
    )
    unitspace_subparsers = unitspace_parser.add_subparsers(
        dest="unitspace_command",
        required=True,
    )
    unitspace_subparsers.add_parser("status", help="Show unitspace estimator status.")

    power_parser = subparsers.add_parser(
        "power",
        help="Inspect Central's shadow power-state decisions.",
        description=(
            "Show derived presence/power state. This does not directly read or switch\n"
            "Node hardware power."
        ),
        formatter_class=_HelpFormatter,
    )
    power_subparsers = power_parser.add_subparsers(
        dest="power_command",
        required=True,
    )
    power_status = power_subparsers.add_parser("status", help="Show shadow power-state status.")
    _add_target_options(power_status, required=False)

    battery_parser = subparsers.add_parser(
        "battery",
        help="Inspect the latest Node battery and uSD REPORT.",
        description=(
            "Show cached USD STATUS fields such as battery, USB/charging, SD mount,\n"
            "active file, and uptime. It does not poll hardware directly."
        ),
        formatter_class=_HelpFormatter,
    )
    battery_subparsers = battery_parser.add_subparsers(
        dest="battery_command",
        required=True,
    )
    battery_status = battery_subparsers.add_parser("status", help="Show latest USD STATUS report.")
    _add_target_options(battery_status, required=False)

    db_parser = subparsers.add_parser(
        "db",
        help="Run or inspect the cron-friendly database synchronization pipeline.",
        description=(
            "Read confirmed EVENT/INFERENCE records from data/**/inference/debugstr,\n"
            "insert them incrementally into local MySQL, and maintain offsets under\n"
            "programdata/db_sync. These commands run without the BLE daemon.\n\n"
            "Remote upload is currently disabled in source for local-only testing."
        ),
        epilog=(
            "Recommended commands:\n"
            "  slimhub-v2 db ingest    # data/ -> local MySQL\n"
            "  slimhub-v2 db status    # inspect config, offsets, and last runs\n"
            "  slimhub-v2 db upload    # currently reports skipped\n\n"
            "'db update' is a compatibility shortcut that runs ingest then upload."
        ),
        formatter_class=_HelpFormatter,
    )
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
    db_update = db_subparsers.add_parser(
        "update",
        help="Compatibility shortcut: run ingest, then the upload stage.",
        description=(
            "Run local ingest followed by upload. The upload stage currently returns\n"
            "skipped because remote DB writes are disabled. Prefer 'db ingest' for\n"
            "local-only operation."
        ),
        formatter_class=_HelpFormatter,
    )
    # Compatibility only: this is exactly equivalent to `db ingest`, so keep
    # old cron/scripts parseable without advertising a duplicate workflow.
    db_update.add_argument("--no-upload", action="store_true", help=argparse.SUPPRESS)
    db_subparsers.add_parser(
        "ingest",
        help="Incrementally insert new debugstr records into local MySQL.",
        description=(
            "Canonical local-only DB command. Source offsets advance only after the\n"
            "corresponding local transaction commits."
        ),
        formatter_class=_HelpFormatter,
    )
    db_subparsers.add_parser(
        "upload",
        help="Run the remote-upload stage (currently disabled/skipped).",
        description=(
            "Attempt the local-to-remote stage. In this branch it performs no remote\n"
            "connection or INSERT and records a skipped result."
        ),
        formatter_class=_HelpFormatter,
    )
    db_subparsers.add_parser(
        "status",
        help="Show DB configuration, offsets, cron evidence, and last results.",
        description=(
            "Print safe operational evidence without passwords. This command does not\n"
            "connect to MySQL or change any offset."
        ),
        formatter_class=_HelpFormatter,
    )

    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    cli_args = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(cli_args)
    if args.legacy_help:
        print(_legacy_help())
        return 0
    if not _has_action(args):
        parser.print_help(sys.stderr)
        return 0

    paths = AppPaths.from_base(args.base_dir)
    _setup_logging(args.debug, paths)

    try:
        if _is_run_command(args):
            target_address = _resolve_local_target(paths, args)
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
            asyncio.run(daemon.run(address=target_address, scan=not args.no_scan))
            return 0

        data: object
        if args.hubconfig:
            data = HubConfigStore(paths).set_field(args.hubconfig[0], args.hubconfig[1]).__dict__
        elif args.subcommand == "db":
            data = _run_database(paths, args)
        elif _should_show_default_wait_eta(args, cli_args):
            data = _send_with_default_wait_eta(paths, args)
        else:
            data = _send(paths, args)
        _print_result(args, data)
        return _result_exit_code(args, data)
    except FileNotFoundError:
        print("Slimhub server is not running")
        return 1 if args.subcommand == "sound" else 0
    except KeyboardInterrupt:
        return _handle_sound_interrupt(paths, args)
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


def _legacy_help() -> str:
    return """SLIMHUB v1 compatibility options

The structured v2 commands are preferred. These flat options remain available
for existing scripts and aliases:

  -r, --run [--background] [scan options]  Start the daemon
  -q, --quit                               Stop the daemon
  -l, --list                               List devices
  -c, --config ADDRESS FIELD VALUE         Set device configuration
  -a, --apply                              Apply device configuration
  -s, --service ADDRESS ACTION SERVICE CHARACTERISTIC
  -f, --feature ADDRESS START_OR_STOP
  --hubconfig KEY VALUE
  --reset ADDRESS
  --model ADDRESS COMMAND
  --file ADDRESS FILE_PATH SAVE_PATH

Modern equivalents include:

  slimhub-v2 run --background
  slimhub-v2 stop
  slimhub-v2 devices
  slimhub-v2 config set --address ADDRESS {type,name,location} VALUE
  slimhub-v2 config apply
  slimhub-v2 command send --address ADDRESS --command {enter,exit}
  slimhub-v2 sound start --address ADDRESS --label LABEL

Run 'slimhub-v2 <command> --help' to see modern command options."""


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
        return send_request_sync(paths, "connect", _target_payload(args))
    if args.subcommand == "command" and args.command_action == "send":
        return send_request_sync(
            paths,
            "command.send",
            {**_target_payload(args), "command": args.nus_command},
        )
    if args.subcommand == "command" and args.command_action == "record":
        return send_request_sync(
            paths,
            "command.send",
            {**_target_payload(args), "command": build_record_command(args.seconds)},
        )
    if args.subcommand == "command" and args.command_action == "record-stop":
        return send_request_sync(
            paths,
            "command.send",
            {**_target_payload(args), "command": RECORD_STOP_COMMAND},
        )
    if args.subcommand == "sound" and args.sound_action == "start":
        command = build_sound_start_command(
            args.label,
            threshold_rms=args.threshold_rms,
            max_seconds=args.max_seconds,
            silence_seconds=args.silence_seconds,
        )
        return send_request_sync(
            paths,
            "sound.capture",
            {
                **_target_payload(args),
                "command": command,
                "wait": args.wait,
                "timeout": (
                    _sound_capture_wait_timeout("start", args.max_seconds)
                    if args.wait
                    else SOUND_NO_WAIT_ACCEPT_SECONDS
                ),
            },
        )
    if args.subcommand == "sound" and args.sound_action == "background":
        command = build_sound_background_command(
            max_seconds=args.max_seconds,
        )
        return send_request_sync(
            paths,
            "sound.capture",
            {
                **_target_payload(args),
                "command": command,
                "wait": args.wait,
                "timeout": (
                    _sound_capture_wait_timeout("background", args.max_seconds)
                    if args.wait
                    else SOUND_NO_WAIT_ACCEPT_SECONDS
                ),
            },
        )
    if args.subcommand == "sound" and args.sound_action == "stop":
        return send_request_sync(
            paths,
            "sound.stop",
            {
                **_target_payload(args),
                "wait": args.wait,
                "timeout": SOUND_STOP_WAIT_SECONDS,
            },
        )
    if args.subcommand == "sound" and args.sound_action == "status":
        return send_request_sync(
            paths,
            "sound.status",
            {**_target_payload(args), "command": SOUND_STATUS_COMMAND},
        )
    if args.subcommand == "sound" and args.sound_action == "catalog":
        return send_request_sync(
            paths,
            "sound.catalog",
            _target_payload(args, required=False),
        )
    if args.subcommand == "sound" and args.sound_action == "automatic":
        command = build_sound_auto_command(
            max_seconds=args.max_seconds,
            silence_seconds=args.silence_seconds,
            open_db=args.open_db,
            close_db=args.close_db,
        )
        return send_request_sync(
            paths,
            "sound.capture",
            {
                **_target_payload(args),
                "command": command,
                "wait": args.wait,
                "timeout": (
                    _sound_capture_wait_timeout("automatic", args.max_seconds)
                    if args.wait
                    else SOUND_NO_WAIT_ACCEPT_SECONDS
                ),
            },
        )
    if args.subcommand == "node" and args.node_action == "status":
        return send_request_sync(
            paths,
            "node.status",
            {**_target_payload(args), "refresh": True},
        )
    if (
        args.subcommand == "node"
        and args.node_action == "config"
        and args.node_config_action == "get"
    ):
        return send_request_sync(paths, "node.config.get", _target_payload(args))
    if (
        args.subcommand == "node"
        and args.node_action == "config"
        and args.node_config_action == "set"
    ):
        return send_request_sync(
            paths,
            "node.config.set",
            {
                **_target_payload(args),
                "node_location": args.node_location,
                "profile": args.profile,
            },
        )
    if (
        args.subcommand == "node"
        and args.node_action == "config"
        and args.node_config_action == "reload"
    ):
        return send_request_sync(paths, "node.config.reload", _target_payload(args))
    if args.subcommand == "config" and args.config_command == "set":
        return send_request_sync(
            paths,
            "config.set",
            {**_config_target_payload(args), "field": args.field, "value": args.value},
        )
    if args.subcommand == "config" and args.config_command == "apply":
        return send_request_sync(paths, "config.apply")
    if args.subcommand == "raw" and args.raw_command == "tail":
        return send_request_sync(
            paths,
            "raw.tail",
            {**_target_payload(args, required=False), "lines": args.lines},
        )
    if args.subcommand == "unitspace" and args.unitspace_command == "status":
        return send_request_sync(paths, "unitspace.status")
    if args.subcommand == "power" and args.power_command == "status":
        return send_request_sync(paths, "power.status", _target_payload(args, required=False))
    if args.subcommand == "battery" and args.battery_command == "status":
        return send_request_sync(paths, "battery.status", _target_payload(args, required=False))
    raise RuntimeError("unhandled CLI command")


def _should_show_default_wait_eta(
    args: argparse.Namespace,
    cli_args: Sequence[str],
) -> bool:
    return bool(
        args.subcommand == "sound"
        and args.sound_action in {"start", "background", "automatic", "stop"}
        and getattr(args, "wait", False)
        and "--wait" not in cli_args
        and "--no-wait" not in cli_args
        and getattr(sys.stderr, "isatty", lambda: False)()
    )


def _send_with_default_wait_eta(
    paths: AppPaths,
    args: argparse.Namespace,
) -> object:
    results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def send_in_background() -> None:
        try:
            results.put((True, _send(paths, args)))
        except BaseException as exc:
            results.put((False, exc))

    worker = threading.Thread(
        target=send_in_background,
        name="sound-cli-wait",
        daemon=True,
    )
    worker.start()

    estimate_seconds, deadline_seconds, estimate_kind = _sound_eta_budget(args)
    started_at = time.monotonic()
    try:
        while worker.is_alive():
            elapsed = time.monotonic() - started_at
            _render_sound_eta(
                elapsed=elapsed,
                estimate_seconds=estimate_seconds,
                deadline_seconds=deadline_seconds,
                estimate_kind=estimate_kind,
            )
            worker.join(timeout=0.2)
    finally:
        _clear_sound_eta()

    succeeded, value = results.get()
    if not succeeded:
        assert isinstance(value, BaseException)
        raise value
    return value


def _sound_eta_budget(args: argparse.Namespace) -> tuple[float, float, str]:
    if args.sound_action == "start":
        estimate = float(SOUND_ARM_WAIT_SECONDS + args.max_seconds)
        deadline = float(_sound_capture_wait_timeout("start", args.max_seconds))
        return estimate, deadline, "upper bound"
    if args.sound_action in {"background", "automatic"}:
        estimate = float(args.max_seconds)
        deadline = float(_sound_capture_wait_timeout("background", args.max_seconds))
        return estimate, deadline, "estimate"
    timeout = float(SOUND_STOP_WAIT_SECONDS)
    return timeout, timeout, "upper bound"


def _sound_capture_wait_timeout(action: str, max_seconds: int) -> int:
    finalize_seconds = max(
        SOUND_FINALIZE_MIN_SECONDS,
        (max_seconds + 1) // 2,
    )
    arm_seconds = SOUND_ARM_WAIT_SECONDS if action == "start" else 0
    return arm_seconds + max_seconds + finalize_seconds


def _render_sound_eta(
    *,
    elapsed: float,
    estimate_seconds: float,
    deadline_seconds: float,
    estimate_kind: str,
) -> None:
    width = 24
    progress = min(max(elapsed / max(estimate_seconds, 0.001), 0.0), 1.0)
    filled = min(width, int(progress * width))
    bar = "#" * filled + "-" * (width - filled)
    if elapsed < estimate_seconds:
        remaining = estimate_seconds - elapsed
        eta = f"ETA {'<=' if estimate_kind == 'upper bound' else '~'} {_format_eta(remaining)}"
    else:
        timeout_remaining = max(0.0, deadline_seconds - elapsed)
        eta = f"finalizing; timeout <= {_format_eta(timeout_remaining)}"
    sys.stderr.write(f"\r\033[2K SOUND WAIT [{bar}] {eta}")
    sys.stderr.flush()


def _clear_sound_eta() -> None:
    sys.stderr.write("\r\033[2K")
    sys.stderr.flush()


def _format_eta(seconds: float) -> str:
    total = max(0, int(seconds + 0.999))
    minutes, remaining_seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:02d}:{remaining_seconds:02d}"


def _target_payload(
    args: argparse.Namespace,
    *,
    required: bool = True,
) -> dict[str, object]:
    address = getattr(args, "address", None)
    location = getattr(args, "location", None)
    if address:
        return {"address": str(address)}
    if location:
        return {"location": str(location)}
    if required:
        raise ValueError("provide exactly one target: --address MAC or --location NAME")
    return {}


def _config_target_payload(args: argparse.Namespace) -> dict[str, object]:
    option_address = getattr(args, "target_address", None)
    location = getattr(args, "target_location", None)
    legacy_address = getattr(args, "legacy_address", None)
    if legacy_address and (option_address or location):
        raise ValueError(
            "do not combine the compatibility positional ADDRESS with --address/--location"
        )
    if option_address:
        return {"address": str(option_address)}
    if location:
        return {"location": str(location)}
    if legacy_address:
        return {"address": str(legacy_address)}
    raise ValueError("provide exactly one target: --address MAC or --location NAME")


def _resolve_local_target(paths: AppPaths, args: argparse.Namespace) -> str | None:
    address = getattr(args, "address", None)
    location = getattr(args, "location", None)
    if not address and not location:
        return None
    return DeviceConfigStore(paths).resolve_target(address=address, location=location)


def _run_database(paths: AppPaths, args: argparse.Namespace) -> dict[str, object]:
    updater = DataDirectoryDatabaseUpdater(paths)
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
    if isinstance(data, dict):
        warnings = data.get("warnings")
        if isinstance(warnings, list):
            for warning in warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
    if args.list_flag or args.subcommand == "devices":
        _print_devices(data)
        return
    if args.subcommand == "raw":
        raw_payload = data if isinstance(data, dict) else {}
        for line in raw_payload.get("lines", []):
            print(line)
        return
    if args.subcommand == "command":
        _print_command_send(data)
        return
    if args.subcommand == "sound":
        if args.sound_action == "status":
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif args.sound_action == "catalog":
            _print_sound_catalog(data)
        else:
            _print_sound_outcome(data)
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
    print(
        f"{'Address':<20}{'Type':<15}{'Name':<15}{'Location':<15}"
        f"{'Connected':<10}{'Conflict':<10}"
    )
    conflict_groups: dict[str, set[str]] = {}
    for item in devices:
        location = str(item.get("location", "undefined"))
        if item.get("location_conflict"):
            conflict_groups.setdefault(location, set()).update(
                str(address)
                for address in item.get("location_conflict_devices", [])
            )
        print(
            f"{str(item.get('address', '')):<20}"
            f"{str(item.get('type') or ''):<15}"
            f"{str(item.get('configured_name') or item.get('name') or ''):<15}"
            f"{location:<15}"
            f"{str(item.get('connected', False)):<10}"
            f"{'YES' if item.get('location_conflict') else '':<10}"
        )
    for location, addresses in conflict_groups.items():
        print(
            f"WARNING: duplicate location {location!r}: {', '.join(sorted(addresses))}. "
            "Commands using --location are blocked until each device has a unique location.",
            file=sys.stderr,
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


def _print_sound_outcome(data: object) -> None:
    payload = data if isinstance(data, dict) else {}
    outcome = payload.get("outcome")
    result = outcome if isinstance(outcome, dict) else {}
    session_value = result.get("session")
    session = session_value if isinstance(session_value, dict) else {}
    status = str(result.get("status") or "failed")
    prefix = {
        "complete": "SOUND COMPLETE",
        "armed": "SOUND ARMED",
        "queued": "SOUND STOP QUEUED",
    }.get(status, "SOUND FAILED")
    fields = [
        f"location={session.get('location', 'undefined')}",
        f"label={session.get('label', 'unknown')}",
        f"cid={session.get('cid') or 'unknown'}",
        f"reason={result.get('reason') or 'unknown'}",
    ]
    if status == "complete":
        fields.extend(
            [
                f"samples={session.get('samples')}",
                f"blocks={session.get('blocks')}",
            ]
        )
    if status == "failed":
        fields.append(f"complete={1 if result.get('complete') else 0}")
    fields.append("storage=node_sd")
    print(f"{prefix} {' '.join(fields)}")


def _print_sound_catalog(data: object) -> None:
    entries = (
        [entry for entry in data if isinstance(entry, dict)]
        if isinstance(data, list)
        else []
    )
    if not entries:
        print("No sound inference catalog entries")
        return
    for entry in entries:
        latest_value = entry.get("last_inference")
        latest = latest_value if isinstance(latest_value, dict) else {}
        print(
            f"ENTRY {entry.get('node_mac')} {entry.get('location')} "
            f"model={entry.get('model')} classes={entry.get('class_count')}"
        )
        print(
            "  last: "
            f"index={latest.get('class_index')} "
            f"label={latest.get('label')} "
            f"semantic={latest.get('semantic')} "
            f"conf={latest.get('confidence')} "
            f"source={latest.get('source')}"
        )
        observed = entry.get("catalog")
        if isinstance(observed, list):
            labels = ", ".join(
                f"{item.get('class_index')}={item.get('label')}"
                for item in observed
                if isinstance(item, dict)
            )
            if labels:
                print(f"  catalog: {labels}")


def _result_exit_code(args: argparse.Namespace, data: object) -> int:
    if args.subcommand != "sound" or args.sound_action in {"status", "catalog"}:
        return 0
    payload = data if isinstance(data, dict) else {}
    outcome = payload.get("outcome")
    if isinstance(outcome, dict):
        return int(outcome.get("exit_code", 0))
    return 1


def _handle_sound_interrupt(paths: AppPaths, args: argparse.Namespace) -> int:
    if (
        args.subcommand != "sound"
        or args.sound_action not in {"start", "background", "automatic"}
        or not getattr(args, "wait", False)
    ):
        return 130
    print("SOUND interrupt: requesting Node stop...", file=sys.stderr)
    try:
        data = send_request_sync(
            paths,
            "sound.stop",
            {
                **_target_payload(args),
                "wait": True,
                "timeout": SOUND_INTERRUPT_STOP_WAIT_SECONDS,
            },
        )
        _print_sound_outcome(data)
        return _result_exit_code(args, data)
    except KeyboardInterrupt:
        print(
            "SOUND warning: local CLI interrupted before stop confirmation; "
            "Node capture may still be running.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(
            f"SOUND warning: stop confirmation failed ({exc}); "
            "Node capture may still be running.",
            file=sys.stderr,
        )
        return 1


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


def _sound_label(value: str) -> str:
    try:
        return validate_sound_label(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _bounded_integer(name: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"{name} must be an integer from {minimum} to {maximum}"
            ) from exc
        if parsed < minimum or parsed > maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be an integer from {minimum} to {maximum}"
            )
        return parsed

    return parse


def _setup_logging(debug: bool, paths: AppPaths) -> None:
    paths.ensure()
    if not logging.getLogger().handlers:
        handler = RotatingFileHandler(
            paths.logging_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        logging.basicConfig(
            handlers=[handler],
            level=logging.INFO,
            format="%(asctime)s: %(levelname)s: %(message)s",
        )
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("slimhub").setLevel(logging.DEBUG if debug else logging.INFO)
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
