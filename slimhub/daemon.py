from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from slimhub.ble.central import BleCentral
from slimhub.ble.registry import DeviceRegistry
from slimhub.ble.scanner import discover_named_devices
from slimhub.config import DEFAULT_DEVICE_TYPE, AppPaths, DeviceConfigStore, HubConfigStore
from slimhub.events import (
    AlertEvent,
    CommandEvent,
    ConnectionStateEvent,
    RawDataEvent,
    ReportEvent,
    StructuredEvent,
)
from slimhub.logging import DisplayWriter, RawDataLogger
from slimhub.multimodal import DeploymentManifestStore, MultimodalReportStore
from slimhub.protocol.nus import (
    DEFAULT_DEVICE_NAME,
    AlertPacket,
    ParsedFrame,
    RawDataPacket,
    ReportPacket,
    VALID_COMMANDS,
    normalize_mac,
    validate_command_payload,
)
from slimhub.power_shadow import ShadowPowerState
from slimhub.unitspace.clock import EventReorderBuffer, NodeClockNormalizer
from slimhub.unitspace.estimator import SimpleUnitspaceEstimator


USD_STATUS_FIELDS = (
    "batt_mv",
    "batt_v",
    "batt_pct",
    "batt_rem_mah",
    "batt_cap_mah",
    "batt_valid",
    "usb",
    "chg",
    "sd",
    "file",
    "uptime",
    "ok",
)
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_NOTIFY_TIMEOUT = 5.0


class SlimHubDaemon:
    def __init__(
        self,
        *,
        paths: AppPaths,
        device_name: str = DEFAULT_DEVICE_NAME,
        scan_timeout: float = 5.0,
        scan_interval: float = 10.0,
        reconnect_delay: float = 3.0,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        notify_timeout: float = DEFAULT_NOTIFY_TIMEOUT,
        logger: logging.Logger | None = None,
    ) -> None:
        self.paths = paths
        self.device_name = device_name
        self.scan_timeout = scan_timeout
        self.scan_interval = scan_interval
        self.reconnect_delay = reconnect_delay
        self.connect_timeout = connect_timeout
        self.notify_timeout = notify_timeout
        self.logger = logger or logging.getLogger(__name__)

        self.config_store = DeviceConfigStore(paths)
        self.hub_config_store = HubConfigStore(paths)
        self.raw_logger = RawDataLogger(paths)
        self.display_writer = DisplayWriter(paths)
        self.estimator = SimpleUnitspaceEstimator()
        self.multimodal = MultimodalReportStore(
            DeploymentManifestStore(paths.deployment_manifest_path)
        )
        self.clock_normalizer = NodeClockNormalizer()
        self.report_reorder_buffer: EventReorderBuffer[ReportEvent] = EventReorderBuffer()
        self._report_reorder_task: asyncio.Task[None] | None = None
        self.power_shadow = ShadowPowerState(paths)
        self.registry = DeviceRegistry()
        self.battery_status: dict[str, dict[str, object]] = {}
        self._session_ids: dict[str, str] = {}
        self._session_generation: dict[str, int] = {}
        self._sound_schemas: dict[str, tuple[str | None, int | None]] = {}
        self.adapter_lock = asyncio.Lock()
        self.central = BleCentral(
            registry=self.registry,
            on_frame=self.handle_frame,
            on_connection_state=self.handle_connection_state,
            on_command_result=self.handle_command_result,
            reconnect_delay=self.reconnect_delay,
            connect_timeout=self.connect_timeout,
            notify_timeout=self.notify_timeout,
            adapter_lock=self.adapter_lock,
            logger=self.logger,
        )
        self.stop_event = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None

    async def run(self, *, address: str | None = None, scan: bool = True) -> None:
        self.paths.ensure()
        self.hub_config_store.load_or_create()
        await self.raw_logger.start()
        await self._start_server()
        self.logger.info(
            "BLE scan settings name=%s scan_timeout=%.1fs scan_interval=%.1fs "
            "connect_timeout=%.1fs notify_timeout=%.1fs",
            self.device_name,
            self.scan_timeout,
            self.scan_interval,
            self.connect_timeout,
            self.notify_timeout,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_stop)

        tasks: list[asyncio.Task[None]] = []
        if address:
            await self.connect_address(address)
        if scan:
            tasks.append(asyncio.create_task(self._scan_loop(), name="ble-scan"))

        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.flush_report_reorder_buffer()
            if self._report_reorder_task is not None:
                self._report_reorder_task.cancel()
                await asyncio.gather(self._report_reorder_task, return_exceptions=True)
                self._report_reorder_task = None
            await self.registry.stop_all()
            await self.raw_logger.stop()
            await self._stop_server()

    def request_stop(self) -> None:
        self.stop_event.set()

    async def connect_address(self, address: str) -> dict[str, object]:
        normalized = normalize_mac(address)
        session = await self.central.ensure_address(normalized)
        self.config_store.ensure(normalized, device_type=session.name or DEFAULT_DEVICE_TYPE)
        return session.status()

    async def send_command(self, address: object, command: object) -> dict[str, object]:
        if not isinstance(address, str) or not address:
            raise ValueError("address is required")
        if not isinstance(command, str):
            raise ValueError(
                "command must be one of: enter, exit, record, record:<seconds>, record_stop"
            )

        normalized_address = normalize_mac(address)
        validated_command = validate_command_payload(command)
        session = await self.registry.get(normalized_address)
        if session is None:
            raise ValueError(
                f"no active device session for address: {normalized_address}"
            )

        config = self.config_store.load(normalized_address)
        sent = await self.registry.send_command(
            CommandEvent(normalized_address, validated_command, config.location)
        )
        if not sent:
            raise ValueError(
                f"no active device session for address: {normalized_address}"
            )
        if validated_command in VALID_COMMANDS:
            self.power_shadow.update_command_hint(
                normalized_address,
                validated_command,
                time.time(),
            )

        return {
            "address": normalized_address,
            "command": validated_command,
            "session": session.status(),
        }

    async def apply_config(self) -> str:
        for config in self.config_store.list_all():
            session = await self.registry.get(config.address)
            if session is not None:
                session.name = config.name or session.name
        return "Config data applied"

    async def service_command(
        self,
        address: object,
        action: object,
        service: object,
        characteristic: object | None = None,
    ) -> str:
        normalized_address = normalize_mac(str(address))
        session = await self.registry.get(normalized_address)
        if session is None:
            return f"{normalized_address} is not registered"
        if not session.status().get("connected", False):
            return f"{normalized_address} is not connected"

        action_text = str(action)
        service_text = str(service)
        characteristic_text = str(characteristic) if characteristic is not None else ""

        if action_text == "enable":
            return (
                f"{normalized_address}: characteristic {service_text} "
                f"{characteristic_text} enabled"
            )
        if action_text == "disable":
            return (
                f"{normalized_address}: characteristic {service_text} "
                f"{characteristic_text} disabled"
            )
        if action_text == "activate":
            return f"{normalized_address}: service {service_text} activated"
        if action_text == "deactivate":
            return f"{normalized_address}: service {service_text} deactivated"
        raise ValueError("service action must be enable, disable, activate or deactivate")

    async def unsupported_device_command(
        self,
        address: object,
        command_name: str,
        detail: str = "",
    ) -> str:
        normalized_address = normalize_mac(str(address))
        session = await self.registry.get(normalized_address)
        if session is None:
            return f"{normalized_address} is not registered"
        suffix = f" {detail}" if detail else ""
        return f"{command_name}{suffix} is not supported by SLIMHUB_v2 NUS-only daemon"

    async def handle_frame(self, source_address: str, frame: ParsedFrame) -> None:
        await self.registry.register_alias(frame.mac, source_address)
        if isinstance(frame.parsed, RawDataPacket):
            config = self.config_store.load(frame.mac)
            self.config_store.save(config)
            timestamp = time.time()
            sound_schema_version, sound_class_count = self._sound_schemas.get(
                normalize_mac(frame.mac), (None, None)
            )
            event = RawDataEvent(
                timestamp=timestamp,
                mac=frame.mac,
                location=config.location,
                packet=frame.parsed,
                payload=frame.payload,
                device_type=config.type,
                source_address=source_address,
                session_id=self._session_ids.get(normalize_mac(source_address)),
                receipt_timestamp=timestamp,
                sound_schema_version=sound_schema_version,
                sound_class_count=sound_class_count,
            )
            self.power_shadow.update_rawdata(frame.mac, frame.parsed, timestamp)
            await self.raw_logger.log(event)
            sent_commands = await self._send_unitspace_commands(
                self.estimator.handle(event)
            )
            await self._log_estimator_records()
            self._log_commands(sent_commands)
        elif isinstance(frame.parsed, AlertPacket):
            config = self.config_store.load(frame.mac)
            self.config_store.save(config)
            timestamp = time.time()
            event = AlertEvent(
                timestamp=timestamp,
                mac=frame.mac,
                location=config.location,
                packet=frame.parsed,
                payload=frame.payload,
                device_type=config.type,
            )
            self.power_shadow.update_alert(frame.mac, frame.parsed.message, timestamp)
            await self.raw_logger.log_alert(event)
        elif isinstance(frame.parsed, ReportPacket):
            config = self.config_store.load(frame.mac)
            self.config_store.save(config)
            timestamp = time.time()
            src = frame.parsed.fields.get("src", "").upper()
            connected = await self._source_connected(source_address)
            boot_id = frame.parsed.fields.get("boot_id")
            event_ts_ms = _int_or_none(frame.parsed.fields.get("event_ts_ms"))
            normalized = self.clock_normalizer.normalize(
                frame.mac,
                boot_id,
                event_ts_ms,
                timestamp,
            )
            report_event = ReportEvent(
                timestamp=timestamp,
                mac=frame.mac,
                source_address=source_address,
                location=config.location,
                packet=frame.parsed,
                payload=frame.payload,
                device_type=config.type,
                connected=connected,
                session_id=self._session_ids.get(normalize_mac(source_address)),
                receipt_timestamp=timestamp,
                clock_offset_ms=normalized.offset_ms,
                clock_error_ms=normalized.error_ms,
                wrap_epoch=normalized.wrap_epoch,
                normalized_timestamp=normalized.timestamp,
            )
            # Reports are diagnostic evidence even when their source is new to
            # this Central build, so retain every well-formed REPORT packet.
            await self.raw_logger.log_report(report_event)
            if src in {"INOUT", "EVENT", "ADL"} and boot_id and event_ts_ms is not None:
                for ordered_event in self.report_reorder_buffer.push(
                    report_event,
                    normalized.timestamp,
                ):
                    await self._process_report_event(ordered_event)
                self._ensure_report_reorder_flush()
            else:
                await self._process_report_event(report_event)
            self._log_report(frame)

    async def handle_connection_state(
        self,
        address: str,
        connected: bool,
        timestamp: float,
    ) -> None:
        if connected:
            normalized = normalize_mac(address)
            generation = self._session_generation.get(normalized, 0) + 1
            self._session_generation[normalized] = generation
            self._session_ids[normalized] = f"{normalized.replace(':', '')}-{generation}"
            self.power_shadow.mark_connected(address, timestamp)
        else:
            self.power_shadow.mark_disconnected(address, timestamp)
        await self.raw_logger.log_connection_state(
            ConnectionStateEvent(
                timestamp=timestamp,
                address=address,
                connected=connected,
                session_id=self._session_ids.get(normalize_mac(address)),
            )
        )
        state = "connected" if connected else "disconnected"
        self.logger.info("%s %s", address, state)

    async def handle_command_result(
        self,
        command: CommandEvent,
        succeeded: bool,
        error: str | None,
        timestamp: float,
    ) -> None:
        # A successful GATT write is transport evidence only. Desired/actual
        # state remains pending until C0/C1 or reconnect STATE reconciliation.
        await self.raw_logger.log_structured(
            StructuredEvent(
                timestamp=timestamp,
                kind="command_write",
                mac=command.address,
                data={
                    "classification": "write_success" if succeeded else "write_failure",
                    "command": command.command,
                    "cmd_id": command.cmd_id,
                    "desired_epoch": command.desired_epoch,
                    "canonical_node_id": command.canonical_node_id or command.address,
                    "ble_address": command.ble_address,
                    "error": error,
                    "ack_pending": True,
                },
            )
        )

    async def _scan_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                async with self.adapter_lock:
                    devices = await discover_named_devices(self.device_name, self.scan_timeout)
                for device in devices:
                    await self._start_or_update_session(device)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("BLE scan failed")
            await self._wait_or_stop(self.scan_interval)

    async def _send_unitspace_commands(
        self,
        commands: list[CommandEvent],
    ) -> list[CommandEvent]:
        sent_commands = []
        for command in commands:
            ble_address = await self.registry.resolve_address(command.address)
            command = replace(command, ble_address=ble_address)
            sent = await self.registry.send_command(command)
            if not sent:
                self.logger.warning(
                    "No active session for command location=%s command=%s",
                    command.location,
                    command.command,
                )
                continue
            sent_commands.append(command)
            self.power_shadow.update_command_hint(
                command.address,
                command.command,
                time.time(),
            )
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=time.time(),
                    kind="command",
                    mac=command.address,
                    data={
                        "classification": "desired_state",
                        "command": command.command,
                        "cmd_id": command.cmd_id,
                        "desired_epoch": command.desired_epoch,
                        "canonical_node_id": command.canonical_node_id or command.address,
                        "ble_address": command.ble_address,
                        "location": command.location,
                        "estimator_state": self.estimator.snapshot(),
                    },
                )
            )
        return sent_commands

    async def _log_estimator_records(self) -> None:
        for record in self.estimator.drain_records():
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=record.timestamp,
                    kind=record.kind,
                    mac=record.mac,
                    data={
                        **record.data,
                        "estimator_state": self.estimator.snapshot(),
                    },
                )
            )

    async def _process_report_event(self, event: ReportEvent) -> None:
        src = event.packet.fields.get("src", "").upper()
        if src == "USD":
            self._remember_usd_status(
                event.mac,
                event.source_address,
                event.location,
                event.device_type,
                event.packet,
                event.timestamp,
                bool(event.connected),
            )
        if src == "INOUT":
            self.multimodal.handle_inout(event)
            self.power_shadow.update_report(event.mac, event.packet, event.timestamp)
            sent_commands = await self._send_unitspace_commands(
                self.estimator.handle_report(event)
            )
            await self._log_estimator_records()
            self._log_commands(sent_commands)
            self.display_writer.write_inout(event)
        if src in {"EVENT", "ADL"}:
            self.multimodal.handle(event)
            await self._log_multimodal_records()
        if src == "SOUND" or (
            src == "EVENT" and event.packet.fields.get("event", "").upper() == "SOUND"
        ):
            self._remember_sound_schema(event.mac, event.packet)

    async def _log_multimodal_records(self) -> None:
        for record in self.multimodal.drain_records():
            errors = record.data.get("errors")
            if isinstance(errors, list) and any("profile" in str(error) for error in errors):
                self.logger.warning(
                    "Deployment profile mismatch mac=%s errors=%s",
                    record.mac,
                    ",".join(str(error) for error in errors),
                )
            self.display_writer.write_multimodal(record)
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=record.timestamp,
                    kind=record.kind,
                    mac=record.mac,
                    data={
                        **record.data,
                        "multimodal_state": self.multimodal.snapshot(),
                    },
                )
            )

    def _ensure_report_reorder_flush(self) -> None:
        if self._report_reorder_task is None or self._report_reorder_task.done():
            self._report_reorder_task = asyncio.create_task(
                self._flush_reports_after_window(),
                name="inout-report-reorder",
            )

    async def _flush_reports_after_window(self) -> None:
        await asyncio.sleep(self.report_reorder_buffer.window_seconds)
        await self.flush_report_reorder_buffer()

    async def flush_report_reorder_buffer(self) -> None:
        for event in self.report_reorder_buffer.flush():
            await self._process_report_event(event)

    async def _source_connected(self, source_address: str) -> bool:
        session = await self.registry.get(source_address)
        if session is None:
            return False
        return bool(session.status().get("connected", False))

    async def _start_or_update_session(self, target: object) -> None:
        address = normalize_mac(str(getattr(target, "address")))
        await self.central.ensure_target(target)
        self.config_store.ensure(
            address,
            device_type=str(getattr(target, "name", "") or DEFAULT_DEVICE_TYPE),
            name=str(getattr(target, "name", "") or ""),
        )

    async def _connected_session_count(self) -> int:
        statuses = await self.registry.list_status()
        return sum(1 for item in statuses if item.get("connected"))

    def _log_commands(self, commands: list[CommandEvent]) -> None:
        if not commands:
            return

        enter_location = None
        exit_location = None
        for command in commands:
            location = command.location or "undefined"
            action = command.command.upper()
            self.logger.info("%s %s", location, action)
            if command.command.endswith("enter"):
                enter_location = location
            elif command.command.endswith("exit"):
                exit_location = location

        if enter_location and exit_location:
            self.logger.info("%s >>> %s", exit_location, enter_location)

    def _log_report(self, frame: ParsedFrame) -> None:
        report = frame.parsed
        if not isinstance(report, ReportPacket):
            return
        src = report.fields.get("src", "").upper()
        if src == "INOUT":
            return

        if src == "USD":
            return

        if src != "SOUND":
            return

        if report.fields.get("err") or report.fields.get("dropped"):
            details = " ".join(
                f"{key}={value}"
                for key in ("event", "path", "dropped", "reason", "err")
                if (value := report.fields.get(key))
            )
            self.logger.warning("SOUND report mac=%s %s", frame.mac, details or report.message)

    async def _start_server(self) -> None:
        socket_path = self.paths.socket_path
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(socket_path),
        )
        self.logger.info("Daemon socket listening at %s", socket_path)

    async def _stop_server(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with suppress(FileNotFoundError):
            self.paths.socket_path.unlink()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            line = await reader.readline()
            response = await self.dispatch(json.loads(line.decode("utf-8")))
        except Exception as exc:
            response = {"ok": False, "data": None, "error": str(exc)}
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def dispatch(self, request: dict[str, object]) -> dict[str, object]:
        command = request.get("command")
        args = request.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("request args must be an object")

        if command == "stop":
            self.request_stop()
            return self._ok({"stopping": True})
        if command == "devices":
            return self._ok(await self._devices_payload())
        if command == "connect":
            return self._ok(await self.connect_address(str(args["address"])))
        if command == "command.send":
            return self._ok(
                await self.send_command(args.get("address"), args.get("command"))
            )
        if command == "config.set":
            config = self.config_store.set_field(
                str(args["address"]),
                str(args["field"]),
                str(args["value"]),
            )
            return self._ok(config.__dict__)
        if command == "config.apply":
            return self._ok(await self.apply_config())
        if command == "hub.config.set":
            config = self.hub_config_store.set_field(
                str(args["field"]),
                str(args["value"]),
            )
            return self._ok(config.__dict__)
        if command == "service":
            return self._ok(
                await self.service_command(
                    args.get("address"),
                    args.get("action"),
                    args.get("service"),
                    args.get("characteristic"),
                )
            )
        if command == "reset":
            return self._ok(
                await self.unsupported_device_command(args.get("address"), "Reset")
            )
        if command == "model":
            return self._ok(
                await self.unsupported_device_command(
                    args.get("address"),
                    "Model",
                    str(args.get("model_command", "")),
                )
            )
        if command == "feature":
            return self._ok(
                await self.unsupported_device_command(
                    args.get("address"),
                    "Feature collection",
                    str(args.get("feature_command", "")),
                )
            )
        if command == "file":
            return self._ok(
                await self.unsupported_device_command(
                    args.get("address"),
                    "File transfer",
                    str(args.get("file_path", "")),
                )
            )
        if command == "unitspace.status":
            return self._ok(self.estimator.snapshot())
        if command == "multimodal.status":
            return self._ok(self.multimodal.snapshot())
        if command == "power.status":
            address = args.get("address")
            return self._ok(
                self.power_shadow.snapshot(str(address)) if address else self.power_shadow.snapshot()
            )
        if command == "battery.status":
            return self._ok(await self._battery_status_payload(args.get("address")))
        if command == "raw.tail":
            address = args.get("address")
            lines = int(args.get("lines", 20))
            return self._ok({"lines": self._tail_raw(str(address) if address else None, lines)})

        raise ValueError(f"unknown command: {command!r}")

    async def _devices_payload(self) -> list[dict[str, object]]:
        statuses = {item["address"]: item for item in await self.registry.list_status()}
        for config in self.config_store.list_all():
            canonical = await self.registry.resolve_address(config.address)
            status = statuses.setdefault(
                canonical,
                {
                    "address": canonical,
                    "name": "",
                    "type": DEFAULT_DEVICE_TYPE,
                    "connected": False,
                    "last_seen": 0.0,
                    "last_error": None,
                    "queued_commands": 0,
                },
            )
            status["packet_address"] = config.address
            status["configured_name"] = config.name
            status["type"] = config.type
            status["location"] = config.location
        return list(statuses.values())

    def _tail_raw(self, address: str | None, lines: int) -> list[str]:
        files = self._raw_files(address)
        if not files:
            return []
        selected = sorted(files, key=lambda path: path.stat().st_mtime)[-1]
        with selected.open("r", encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f.readlines()[-lines:]]

    def _raw_files(self, address: str | None) -> list[Path]:
        if address is None:
            return list(self.paths.data_dir.glob("*/*/*/inference/rawdata/*.txt"))
        mac = normalize_mac(address)
        return list(self.paths.data_dir.glob(f"*/*/{mac}/inference/rawdata/*.txt"))

    async def _wait_or_stop(self, delay_seconds: float) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay_seconds)

    def _ok(self, data: object) -> dict[str, object]:
        return {"ok": True, "data": data, "error": None}

    def _remember_usd_status(
        self,
        mac: str,
        source_address: str,
        location: str,
        device_type: str,
        report: ReportPacket,
        timestamp: float,
        connected: bool,
    ) -> None:
        fields = dict(report.fields)
        status: dict[str, object] = {
            "address": normalize_mac(mac),
            "ble_address": normalize_mac(source_address),
            "location": location,
            "device_type": device_type,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)),
            "timestamp": timestamp,
            "connected": connected,
            "src": fields.get("src", ""),
            "event": fields.get("event", ""),
            "fields": fields,
        }
        for key in USD_STATUS_FIELDS:
            if key in fields:
                status[key] = _coerce_report_value(fields[key])
        self.battery_status[normalize_mac(mac)] = status

    async def _battery_status_payload(self, address: object | None) -> object:
        if address is None:
            return [
                dict(status)
                for _, status in sorted(self.battery_status.items())
            ]

        normalized = normalize_mac(str(address))
        if normalized in self.battery_status:
            return dict(self.battery_status[normalized])
        for status in self.battery_status.values():
            if status.get("ble_address") == normalized:
                return dict(status)
        return {}

    def _remember_sound_schema(self, mac: str, report: ReportPacket) -> None:
        fields = report.fields
        version = fields.get("schema_version") or fields.get("sound_schema")
        if version is None and fields.get("schema") == "1":
            version = "b-tflm-v1"
        class_count = _int_or_none(fields.get("class_count"))
        if version is None and class_count is None:
            return
        # The logger validates this pair when it next writes RAWDATA. Keeping
        # the report visible even when firmware is malformed is intentional.
        self._sound_schemas[normalize_mac(mac)] = (version, class_count)


def _coerce_report_value(value: str) -> object:
    text = value.strip()
    if text == "":
        return ""
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return None
