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
from slimhub.config import (
    DEFAULT_DEVICE_TYPE,
    DEFAULT_LOCATION,
    AppPaths,
    DeviceConfigStore,
    HubConfigStore,
    is_assigned_location,
    location_key,
    normalize_node_location,
)
from slimhub.dean_contract import (
    DeanContractStore,
    build_config_set_command,
    build_time_sync_command,
)
from slimhub.events import (
    AlertEvent,
    CommandEvent,
    ConnectionStateEvent,
    RawDataEvent,
    ReportEvent,
    StructuredEvent,
)
from slimhub.logging import (
    DisplayWriter,
    LegacyReportValidationError,
    LegacyReportWriter,
    RawDataLogger,
    validate_legacy_report,
)
from slimhub.location_sync import LocationSyncCoordinator
from slimhub.multimodal import DeploymentManifestStore, MultimodalReportStore
from slimhub.protocol.nus import (
    DEFAULT_DEVICE_NAME,
    AlertPacket,
    IgnoredPacket,
    ParsedFrame,
    RawDataPacket,
    ReportPacket,
    VALID_COMMANDS,
    normalize_mac,
    validate_command_payload,
)
from slimhub.power_shadow import ShadowPowerState
from slimhub.sound_capture import SoundCaptureStore
from slimhub.sound_inference import SoundInferenceStore
from slimhub.singleton import DaemonInstanceLock
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
SOUND_STATUS_WAIT_SECONDS = 2.0
DEFAULT_SOUND_ACCEPT_WAIT_SECONDS = 180.0
DEFAULT_SOUND_CAPTURE_WAIT_SECONDS = 3600.0
DEFAULT_SOUND_STOP_WAIT_SECONDS = 1020.0


def _fill_sound_outcome_location(outcome: dict[str, object], location: str) -> None:
    session = outcome.get("session")
    if isinstance(session, dict) and session.get("location") in {None, "", "undefined"}:
        session["location"] = location


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
        self.legacy_report_writer = LegacyReportWriter(paths)
        self.estimator = SimpleUnitspaceEstimator()
        self.multimodal = MultimodalReportStore(
            DeploymentManifestStore(paths.deployment_manifest_path)
        )
        self.clock_normalizer = NodeClockNormalizer()
        self.report_reorder_buffer: EventReorderBuffer[ReportEvent] = EventReorderBuffer()
        self._report_reorder_task: asyncio.Task[None] | None = None
        self.power_shadow = ShadowPowerState(paths)
        self.dean_contract = DeanContractStore(paths.node_state_path)
        self.location_sync = LocationSyncCoordinator()
        self.sound_inference = SoundInferenceStore(paths.sound_inference_db_path)
        self.registry = DeviceRegistry()
        self.battery_status: dict[str, dict[str, object]] = {}
        self._session_ids: dict[str, str] = {}
        self._session_generation: dict[str, int] = {}
        self._sound_schemas: dict[str, tuple[str | None, int | None]] = {}
        self.sound_capture = SoundCaptureStore(paths, self.logger)
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
        self._instance_lock = DaemonInstanceLock(paths.daemon_lock_path)

    async def run(self, *, address: str | None = None, scan: bool = True) -> None:
        self._instance_lock.acquire()
        try:
            await self._run_locked(address=address, scan=scan)
        finally:
            self._instance_lock.release()

    async def _run_locked(
        self,
        *,
        address: str | None = None,
        scan: bool = True,
    ) -> None:
        self.paths.ensure()
        self.display_writer.ensure()
        self.hub_config_store.load_or_create()
        await self.raw_logger.start()
        await self.legacy_report_writer.start()
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
        tasks.append(
            asyncio.create_task(
                self._occupancy_timeout_loop(),
                name="occupancy-timeout",
            )
        )
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
            await self.legacy_report_writer.stop()
            await self.raw_logger.stop()
            await self._stop_server()

    def request_stop(self) -> None:
        self.stop_event.set()

    async def connect_address(self, address: str) -> dict[str, object]:
        normalized = normalize_mac(address)
        session = await self.central.ensure_address(normalized)
        self.config_store.ensure(normalized, device_type=session.name or DEFAULT_DEVICE_TYPE)
        return session.status()

    def resolve_device_target(
        self,
        address: object | None,
        location: object | None,
    ) -> str:
        return self.config_store.resolve_target(address=address, location=location)

    async def send_command(
        self,
        address: object,
        command: object,
        *,
        location: object | None = None,
    ) -> dict[str, object]:
        if not isinstance(command, str):
            raise ValueError(
                "command must be a supported NUS COMMAND payload"
            )

        normalized_address = self.resolve_device_target(address, location)
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
        sound_request_id = self.sound_capture.register_command(
            normalized_address,
            validated_command,
            time.time(),
        )
        if validated_command in VALID_COMMANDS:
            self.power_shadow.update_command_hint(
                normalized_address,
                validated_command,
                time.time(),
            )

        result = {
            "address": normalized_address,
            "location": config.location,
            "command": validated_command,
            "session": session.status(),
        }
        if sound_request_id is not None:
            result["sound_request_id"] = sound_request_id
        return result

    def set_device_config(
        self,
        address: object,
        location: object,
        field: object,
        value: object,
    ) -> dict[str, object]:
        normalized_address = self.resolve_device_target(address, location)
        config, warning = self.config_store.set_field_unique(
            normalized_address,
            str(field),
            str(value),
        )
        result = dict(config.__dict__)
        result["warnings"] = [warning] if warning else []
        if warning:
            self.logger.warning("Device configuration warning: %s", warning)
        return result

    async def apply_config(self) -> str:
        for config in self.config_store.list_all():
            session = await self.registry.get(config.address)
            if session is not None:
                session.name = config.name or session.name
        return "Config data applied"

    async def node_status(
        self,
        address: object,
        *,
        location: object | None = None,
        refresh: bool = True,
    ) -> dict[str, object]:
        normalized = self.resolve_device_target(address, location)
        request = (
            await self.send_command(normalized, "node_status")
            if refresh
            else None
        )
        return {
            "request": request,
            "cached": self.dean_contract.snapshot(normalized),
            "sound_inference": self.sound_inference.snapshot(normalized),
            "location_sync": self.location_sync.snapshot(normalized),
            "writer_health": {
                "rawdata": self.raw_logger.snapshot(),
                "legacy_report": self.legacy_report_writer.snapshot(),
            },
        }

    async def node_config_get(
        self,
        address: object,
        *,
        location: object | None = None,
    ) -> dict[str, object]:
        normalized = self.resolve_device_target(address, location)
        request = await self.send_command(normalized, "config_get")
        return {
            "request": request,
            "cached": self.dean_contract.snapshot(normalized),
        }

    async def node_config_set(
        self,
        address: object,
        node_location: object,
        profile: object | None,
        *,
        target_location: object | None = None,
    ) -> dict[str, object]:
        normalized = self.resolve_device_target(address, target_location)
        location_text = str(node_location)
        profile_text = str(profile) if profile is not None else None
        self.dean_contract.validate_config_change(
            normalized,
            location_text,
            profile_text,
        )
        payload = build_config_set_command(location_text, profile_text)
        request = await self.send_command(normalized, payload)
        return {
            "request": request,
            "cached": self.dean_contract.snapshot(normalized),
            "pending": {
                "location": location_text.strip().upper(),
                "profile": (
                    profile_text.strip().lower()
                    if profile_text is not None
                    else "node-derived"
                ),
                "applies_on": "CONFIG/APPLIED",
            },
        }

    async def node_config_reload(
        self,
        address: object,
        *,
        location: object | None = None,
    ) -> dict[str, object]:
        normalized = self.resolve_device_target(address, location)
        self.dean_contract.validate_config_reload(normalized)
        request = await self.send_command(normalized, "config_reload")
        return {
            "request": request,
            "cached": self.dean_contract.snapshot(normalized),
        }

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
        if (
            isinstance(frame.parsed, ReportPacket)
            and frame.parsed.format == "json"
        ):
            await self._handle_legacy_json_frame(source_address, frame)
            return
        await self.registry.register_alias(frame.mac, source_address)
        if isinstance(frame.parsed, IgnoredPacket):
            self.logger.debug(
                "Ignoring migration packet type=%s mac=%s bytes=%d",
                frame.parsed.packet_type,
                frame.mac,
                frame.parsed.payload_bytes,
            )
        elif isinstance(frame.parsed, RawDataPacket):
            config = self.config_store.load(frame.mac)
            self.config_store.save(config)
            timestamp = time.time()
            sound_schema_version, sound_class_count = self._sound_schemas.get(
                normalize_mac(frame.mac), (None, None)
            )
            node_state = self.dean_contract.node_state(frame.mac)
            event_location = (
                config.location
                if is_assigned_location(config.location)
                else DEFAULT_LOCATION
            )
            semantic_ready = (
                str(node_state.semantic or "").lower() in {"1", "true", "ready"}
                and str(node_state.config or "").upper() == "READY"
            )
            raw_event = RawDataEvent(
                timestamp=timestamp,
                mac=frame.mac,
                location=event_location,
                packet=frame.parsed,
                payload=frame.payload,
                device_type=config.type,
                source_address=source_address,
                session_id=self._session_ids.get(normalize_mac(source_address)),
                receipt_timestamp=timestamp,
                sound_schema_version=sound_schema_version,
                sound_class_count=sound_class_count,
                sound_semantic_ready=(
                    semantic_ready
                    if node_state.semantic is not None or node_state.config is not None
                    else None
                ),
                sound_profile=node_state.profile,
                sound_model=node_state.model,
                sound_raw_schema=node_state.raw_schema,
            )
            self.power_shadow.update_rawdata(frame.mac, frame.parsed, timestamp)
            contract_commands = self.dean_contract.handle_raw(raw_event)
            await self.raw_logger.log(raw_event)
            self.estimator.handle(raw_event)
            sent_commands = await self._send_unitspace_commands(contract_commands)
            if frame.parsed.flag_sound == 1:
                node = self.dean_contract.node_state(frame.mac)
                class_count = node.class_count
                scores = (
                    list(frame.parsed.sound[:14])
                    if node.raw_schema == 2
                    else (
                        list(frame.parsed.sound[:class_count])
                        if class_count is not None and 1 <= class_count <= 16
                        else []
                    )
                )
                await self.raw_logger.log_structured(
                    StructuredEvent(
                        timestamp=timestamp,
                        kind="raw_sound_metadata",
                        mac=frame.mac,
                        data={
                            "raw_scores": scores,
                            "class_count": class_count,
                            "semantic": node.semantic,
                            "profile": node.profile,
                            "location": node.location or config.location,
                            "model": node.model,
                            "raw_schema": node.raw_schema,
                            "semantic_ready": (
                                str(node.semantic or "").lower() in {"1", "true", "ready"}
                                and str(node.config or "").upper() == "READY"
                            ),
                        },
                    )
                )
                if node.raw_schema != 2:
                    await self.raw_logger.log_structured(
                        StructuredEvent(
                            timestamp=timestamp,
                            kind="sound_raw_schema_unconfirmed",
                            mac=frame.mac,
                            data={
                                "reason": (
                                    "profile tensor mapped into the 24-column "
                                    "home union; unknown semantics are zero-filled"
                                ),
                                "raw_schema": node.raw_schema,
                                "location": event_location,
                            },
                        )
                    )
            await self._log_estimator_records()
            await self._log_contract_records()
            self._log_commands(sent_commands)
        elif isinstance(frame.parsed, AlertPacket):
            config = self.config_store.load(frame.mac)
            self.config_store.save(config)
            timestamp = time.time()
            alert_event = AlertEvent(
                timestamp=timestamp,
                mac=frame.mac,
                location=config.location,
                packet=frame.parsed,
                payload=frame.payload,
                device_type=config.type,
            )
            self.power_shadow.update_alert(frame.mac, frame.parsed.message, timestamp)
            await self.raw_logger.log_alert(alert_event)
        elif isinstance(frame.parsed, ReportPacket):
            config = self.config_store.load(frame.mac)
            self.config_store.save(config)
            timestamp = time.time()
            src = frame.parsed.fields.get("src", "").upper()
            connected = await self._source_connected(source_address)
            identity_warning = None
            boot_id = frame.parsed.fields.get("boot_id") or frame.parsed.fields.get("bid")
            event_ts_ms = _int_or_none(
                frame.parsed.fields.get("event_ts_ms") or frame.parsed.fields.get("ts")
            )
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
                identity_warning=identity_warning,
            )
            # Reports are diagnostic evidence even when their source is new to
            # this Central build, so retain every well-formed REPORT packet.
            await self.raw_logger.log_report(report_event)
            report_name = frame.parsed.fields.get("event", "").upper()
            immediate_confirmation = (
                src == "INOUT"
                and report_name
                in {"ENTER", "EXIT", "CONFIRM_ACK", "CONFIRM_ERROR"}
            )
            orderable = (
                not immediate_confirmation
                and (
                    src in {"INOUT", "EVENT", "ADL"}
                    or frame.parsed.format == "json"
                )
            )
            if orderable and boot_id and event_ts_ms is not None:
                for ordered_event in self.report_reorder_buffer.push(
                    report_event,
                    normalized.timestamp,
                ):
                    await self._process_report_event(ordered_event)
                self._ensure_report_reorder_flush()
            else:
                await self._process_report_event(report_event)
            self._log_report(frame)

    @staticmethod
    def _json_identity_warning(frame: ParsedFrame) -> str | None:
        packet = frame.parsed
        if not isinstance(packet, ReportPacket) or packet.format != "json":
            return None
        device = packet.fields.get("device")
        if not device:
            return "missing_device"
        try:
            normalized = normalize_mac(device)
        except ValueError:
            return f"invalid_device:{device}"
        if normalized != normalize_mac(frame.mac):
            return f"device_mismatch:{normalized}"
        return None

    async def _handle_legacy_json_frame(
        self,
        source_address: str,
        frame: ParsedFrame,
    ) -> None:
        packet = frame.parsed
        assert isinstance(packet, ReportPacket)
        timestamp = time.time()
        config = self.config_store.load(frame.mac)
        connected = await self._source_connected(source_address)
        identity_warning = self._json_identity_warning(frame)
        report_event = ReportEvent(
            timestamp=timestamp,
            mac=frame.mac,
            source_address=source_address,
            location=config.location,
            packet=packet,
            payload=frame.payload,
            device_type=config.type,
            connected=connected,
            session_id=self._session_ids.get(normalize_mac(source_address)),
            receipt_timestamp=timestamp,
            identity_warning=identity_warning,
        )
        await self.raw_logger.log_report(report_event)

        reject_reason = packet.parse_error or identity_warning
        validated = None
        if reject_reason is None:
            try:
                validated = validate_legacy_report(frame.mac, packet.document)
            except LegacyReportValidationError as exc:
                reject_reason = str(exc)

        if reject_reason is not None or validated is None:
            self.logger.warning(
                "Legacy REPORT rejected mac=%s type=%s declared=%d actual=%d reason=%s",
                frame.mac,
                frame.packet_type,
                frame.packet_length,
                len(frame.payload),
                reject_reason,
            )
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=timestamp,
                    kind="legacy_report_rejected",
                    mac=frame.mac,
                    data={
                        "frame_type": frame.packet_type,
                        "declared_length": frame.packet_length,
                        "actual_length": len(frame.payload),
                        "reason": reject_reason or "validation_failed",
                        "location": config.location,
                    },
                )
            )
            return

        outcome = await self.legacy_report_writer.log(
            validated,
            timestamp=timestamp,
            location=config.location,
            device_type=config.type,
            wait_for_commit=validated.kind == "DEBUG",
        )
        if outcome in {"written", "deduplicated"} and validated.kind == "DEBUG":
            commands = self.dean_contract.handle_legacy_debug_committed(
                validated.mac,
                str(validated.document.get("event") or ""),
                timestamp,
            )
            if commands:
                sent_commands = await self._send_unitspace_commands(commands)
                self._log_commands(sent_commands)
            await self._log_contract_records()
        if outcome in {"unknown_location", "queue_full", "write_error"}:
            self.logger.warning(
                "Legacy REPORT not stored mac=%s location=%s reason=%s",
                frame.mac,
                config.location,
                outcome,
            )
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=timestamp,
                    kind="legacy_report_rejected",
                    mac=frame.mac,
                    data={
                        "frame_type": frame.packet_type,
                        "declared_length": frame.packet_length,
                        "actual_length": len(frame.payload),
                        "reason": outcome,
                        "location": config.location,
                    },
                )
            )

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
            self.sound_capture.handle_disconnect(address, timestamp)
        self.dean_contract.handle_connection(address, connected, timestamp)
        desired_location = self.config_store.load(address).location
        self.location_sync.handle_connection(
            address,
            connected,
            desired_location,
            timestamp,
        )
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
        if connected:
            await self._initialize_node_session(address, timestamp)

    async def handle_command_result(
        self,
        command: CommandEvent,
        succeeded: bool,
        error: str | None,
        timestamp: float,
    ) -> None:
        # A successful GATT write is transport evidence only. IN/OUT remains
        # pending until the correlated CONFIRM_ACK arrives. Transport retries
        # preserve the exact confirmation identity; explicit CONFIRM_ERROR is
        # terminal.
        self.dean_contract.handle_command_write_result(
            command,
            succeeded,
            error,
            timestamp,
        )
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
        self.sound_capture.handle_command_result(
            command.address,
            command.command,
            succeeded,
            error,
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

    async def _occupancy_timeout_loop(self) -> None:
        while not self.stop_event.is_set():
            timestamp = time.time()
            commands = self.dean_contract.expire_confirmations(timestamp)
            commands.extend(
                self.dean_contract.expire_occupancy(timestamp)
            )
            sent = await self._send_unitspace_commands(commands)
            self._log_commands(sent)
            await self._log_contract_records()
            await self._wait_or_stop(1.0)

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
            if command.command in VALID_COMMANDS:
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

    async def _initialize_node_session(self, address: str, timestamp: float) -> None:
        """Queue the once-per-link DEAN initialization sequence after notify setup."""
        config = self.config_store.load(address)
        for payload in (build_time_sync_command(timestamp), "node_status", "config_get"):
            sent = await self.registry.send_command(
                CommandEvent(
                    address=normalize_mac(address),
                    command=payload,
                    location=config.location,
                    canonical_node_id=normalize_mac(address),
                    created_at=timestamp,
                )
            )
            if not sent:
                self.logger.warning(
                    "Unable to queue Node initialization address=%s command=%s",
                    address,
                    payload.split(",", 1)[0],
                )
                return

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
        if event.packet.format == "json":
            self.multimodal.handle_legacy_json(event)
            await self._log_multimodal_records()
            return
        src = event.packet.fields.get("src", "").upper()
        contract_commands = self.dean_contract.handle_report(event)
        if src in {"NODE", "CONFIG"}:
            self._remember_sound_schema(event.mac, event.packet)
        config = self.config_store.load(event.mac)
        central_location = normalize_node_location(config.location)
        report_location = normalize_node_location(
            event.packet.fields.get("location")
        )
        if (
            central_location is not None
            and report_location is not None
            and central_location != report_location
        ):
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=event.timestamp,
                    kind="location_report_mismatch",
                    mac=event.mac,
                    data={
                        "central_location": central_location,
                        "report_location": report_location,
                        "src": src,
                        "event": event.packet.fields.get("event"),
                    },
                )
            )
        location_sync_commands = self.location_sync.handle_report(
            event,
            self.dean_contract.node_state(event.mac),
            config.location,
        )
        if contract_commands:
            sent_contract_commands = await self._send_unitspace_commands(
                contract_commands
            )
            self._log_commands(sent_contract_commands)
        if location_sync_commands:
            sent_location_commands = await self._send_unitspace_commands(
                location_sync_commands
            )
            self._log_commands(sent_location_commands)
        await self._log_contract_records()
        await self._log_location_sync_records()
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
            self.sound_capture.remember_firmware_status(
                event.mac,
                event.source_address,
                event.packet.fields,
            )
        if src == "INOUT":
            self.multimodal.handle_inout(event)
            self.power_shadow.update_report(event.mac, event.packet, event.timestamp)
            self.estimator.handle_report(event)
            sent_commands: list[CommandEvent] = []
            await self._log_estimator_records()
            self._log_commands(sent_commands)
            self.display_writer.write_inout(event)
            # Session-boundary and host-derived inference records are produced
            # by handle_inout as well as by EVENT/ADL reports.
            await self._log_multimodal_records()
        if src in {"EVENT", "ADL"}:
            self.multimodal.handle(event)
            await self._log_multimodal_records()
        if (
            src == "SOUND"
            and event.packet.fields.get("event", "").upper() == "INFERENCE"
        ):
            await self._handle_sound_inference(event)
        if src == "SOUND" or (
            src == "EVENT" and event.packet.fields.get("event", "").upper() == "SOUND"
        ):
            if src == "SOUND":
                self.sound_capture.handle_report(event)
            self._remember_sound_schema(event.mac, event.packet)

    async def _handle_sound_inference(self, event: ReportEvent) -> None:
        outcome = self.sound_inference.handle_report(
            event,
            self.dean_contract.node_state(event.mac),
        )
        for diagnostic in outcome.diagnostics:
            self.logger.warning(
                "SOUND/INFERENCE diagnostic mac=%s reason=%s raw=%s",
                normalize_mac(event.mac),
                diagnostic.reason,
                event.packet.message,
            )
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=event.timestamp,
                    kind=diagnostic.kind,
                    mac=event.mac,
                    data={
                        "reason": diagnostic.reason,
                        "raw_payload": event.packet.message,
                    },
                )
            )
        if outcome.inference is not None:
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=event.timestamp,
                    kind=(
                        "sound_inference_stored"
                        if outcome.stored
                        else "sound_inference_duplicate"
                    ),
                    mac=event.mac,
                    data={
                        "inference": outcome.inference.__dict__,
                        "stored": outcome.stored,
                        "duplicate": outcome.duplicate,
                    },
                )
            )

    async def _log_contract_records(self) -> None:
        for record in self.dean_contract.drain_records():
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=record.timestamp,
                    kind=record.kind,
                    mac=record.mac,
                    data=dict(record.data),
                )
            )

    async def _log_location_sync_records(self) -> None:
        for record in self.location_sync.drain_records():
            await self.raw_logger.log_structured(
                StructuredEvent(
                    timestamp=record.timestamp,
                    kind=record.kind,
                    mac=record.mac,
                    data=dict(record.data),
                )
            )

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
                    # The record already contains its session identity and
                    # linkage. Repeating the full accumulated session history
                    # here made the append-only JSONL grow quadratically.
                    data=dict(record.data),
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

        if any(
            report.fields.get(key)
            for key in ("err", "dropped", "queue_drop", "ble_drop")
        ):
            details = " ".join(
                f"{key}={value}"
                for key in (
                    "event",
                    "path",
                    "dropped",
                    "queue_drop",
                    "ble_drop",
                    "reason",
                    "err",
                )
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
            target = self.resolve_device_target(
                args.get("address"),
                args.get("location"),
            )
            return self._ok(await self.connect_address(target))
        if command == "command.send":
            return self._ok(
                await self.send_command(
                    args.get("address"),
                    args.get("command"),
                    location=args.get("location"),
                )
            )
        if command == "sound.capture":
            address = self.resolve_device_target(
                args.get("address"),
                args.get("location"),
            )
            request_result = await self.send_command(address, args.get("command"))
            request_id = int(str(request_result["sound_request_id"]))
            wait_for_terminal = bool(args.get("wait", True))
            timeout = float(
                args.get(
                    "timeout",
                    (
                        DEFAULT_SOUND_CAPTURE_WAIT_SECONDS
                        if wait_for_terminal
                        else DEFAULT_SOUND_ACCEPT_WAIT_SECONDS
                    ),
                )
            )
            outcome = await self.sound_capture.wait_for_request(
                request_id,
                terminal=wait_for_terminal,
                timeout=timeout,
            )
            _fill_sound_outcome_location(outcome, str(request_result["location"]))
            return self._ok({"request": request_result, "outcome": outcome})
        if command == "sound.stop":
            address = self.resolve_device_target(
                args.get("address"),
                args.get("location"),
            )
            previous_revision = self.sound_capture.status_revision(address)
            request_result = await self.send_command(address, "sound_stop")
            if not bool(args.get("wait", True)):
                return self._ok(
                    {
                        "request": request_result,
                        "outcome": {
                            "status": "queued",
                            "success": True,
                            "exit_code": 0,
                            "reason": "stop_queued",
                            "complete": None,
                            "session": self.sound_capture.latest_session(address),
                        },
                    }
                )
            outcome = await self.sound_capture.wait_for_terminal_after(
                address,
                after=previous_revision,
                timeout=float(args.get("timeout", DEFAULT_SOUND_STOP_WAIT_SECONDS)),
            )
            _fill_sound_outcome_location(outcome, str(request_result["location"]))
            return self._ok({"request": request_result, "outcome": outcome})
        if command == "sound.status":
            address = self.resolve_device_target(
                args.get("address"),
                args.get("location"),
            )
            previous_revision = self.sound_capture.status_revision(address)
            request_result = await self.send_command(address, "sound_status")
            fresh_report = await self.sound_capture.wait_for_status_revision(
                address,
                after=previous_revision,
                timeout=SOUND_STATUS_WAIT_SECONDS,
            )
            return self._ok(
                {
                    "request": request_result,
                    "fresh_report": fresh_report,
                    "capture": self.sound_capture.snapshot(str(address)),
                }
            )
        if command == "sound.snapshot":
            optional_address = self.resolve_device_target(
                args.get("address"),
                args.get("location"),
            ) if args.get("address") or args.get("location") else None
            return self._ok(
                self.sound_capture.snapshot(
                    str(optional_address) if optional_address else None
                )
            )
        if command == "sound.catalog":
            optional_address = (
                self.resolve_device_target(
                    args.get("address"),
                    args.get("location"),
                )
                if args.get("address") or args.get("location")
                else None
            )
            return self._ok(
                self.sound_inference.snapshot(
                    str(optional_address) if optional_address else None
                )
            )
        if command == "node.status":
            return self._ok(
                await self.node_status(
                    args.get("address"),
                    location=args.get("location"),
                    refresh=bool(args.get("refresh", True)),
                )
            )
        if command == "node.config.get":
            return self._ok(
                await self.node_config_get(
                    args.get("address"),
                    location=args.get("location"),
                )
            )
        if command == "node.config.set":
            return self._ok(
                await self.node_config_set(
                    args.get("address"),
                    args.get("node_location"),
                    args.get("profile"),
                    target_location=args.get("location"),
                )
            )
        if command == "node.config.reload":
            return self._ok(
                await self.node_config_reload(
                    args.get("address"),
                    location=args.get("location"),
                )
            )
        if command == "config.set":
            return self._ok(
                self.set_device_config(
                    args.get("address"),
                    args.get("location"),
                    args.get("field"),
                    args.get("value"),
                )
            )
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
            return self._ok(
                {
                    **self.estimator.snapshot(),
                    "home_token": self.dean_contract.home_snapshot(),
                }
            )
        if command == "multimodal.status":
            return self._ok(self.multimodal.snapshot())
        if command == "power.status":
            optional_address = self.resolve_device_target(
                args.get("address"),
                args.get("location"),
            ) if args.get("address") or args.get("location") else None
            return self._ok(
                self.power_shadow.snapshot(str(optional_address))
                if optional_address
                else self.power_shadow.snapshot()
            )
        if command == "battery.status":
            optional_address = self.resolve_device_target(
                args.get("address"),
                args.get("location"),
            ) if args.get("address") or args.get("location") else None
            return self._ok(await self._battery_status_payload(optional_address))
        if command == "raw.tail":
            optional_address = self.resolve_device_target(
                args.get("address"),
                args.get("location"),
            ) if args.get("address") or args.get("location") else None
            lines = int(str(args.get("lines", 20)))
            return self._ok(
                {
                    "lines": self._tail_raw(
                        str(optional_address) if optional_address else None,
                        lines,
                    )
                }
            )

        raise ValueError(f"unknown command: {command!r}")

    async def _devices_payload(self) -> list[dict[str, object]]:
        statuses = {item["address"]: item for item in await self.registry.list_status()}
        configs = self.config_store.list_all()
        locations: dict[str, list[str]] = {}
        for config in configs:
            if is_assigned_location(config.location):
                locations.setdefault(location_key(config.location), []).append(config.address)
        for config in configs:
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
            conflicting_addresses = locations.get(location_key(config.location), [])
            status["location_conflict"] = len(conflicting_addresses) > 1
            status["location_conflict_devices"] = conflicting_addresses
            status["writer_health"] = {
                "rawdata": self.raw_logger.snapshot(),
                "legacy_report": self.legacy_report_writer.snapshot(),
            }
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
        version = (
            fields.get("profile")
            or fields.get("sound_profile")
            or fields.get("schema_version")
            or fields.get("sound_schema")
        )
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
