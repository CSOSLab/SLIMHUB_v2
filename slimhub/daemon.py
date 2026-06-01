from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from contextlib import suppress
from pathlib import Path

from slimhub.ble.central import BleCentral
from slimhub.ble.registry import DeviceRegistry
from slimhub.ble.scanner import discover_named_devices
from slimhub.config import DEFAULT_DEVICE_TYPE, AppPaths, DeviceConfigStore, HubConfigStore
from slimhub.events import AlertEvent, CommandEvent, RawDataEvent
from slimhub.logging import RawDataLogger
from slimhub.protocol.nus import (
    DEFAULT_DEVICE_NAME,
    AlertPacket,
    ParsedFrame,
    RawDataPacket,
    normalize_mac,
    validate_command,
)
from slimhub.unitspace import SimpleUnitspaceEstimator


class SlimHubDaemon:
    def __init__(
        self,
        *,
        paths: AppPaths,
        device_name: str = DEFAULT_DEVICE_NAME,
        scan_timeout: float = 5.0,
        scan_interval: float = 10.0,
        reconnect_delay: float = 3.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.paths = paths
        self.device_name = device_name
        self.scan_timeout = scan_timeout
        self.scan_interval = scan_interval
        self.reconnect_delay = reconnect_delay
        self.logger = logger or logging.getLogger(__name__)

        self.config_store = DeviceConfigStore(paths)
        self.hub_config_store = HubConfigStore(paths)
        self.raw_logger = RawDataLogger(paths)
        self.estimator = SimpleUnitspaceEstimator()
        self.registry = DeviceRegistry()
        self.adapter_lock = asyncio.Lock()
        self.central = BleCentral(
            registry=self.registry,
            on_frame=self.handle_frame,
            reconnect_delay=self.reconnect_delay,
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
                "command must be one of: strong_enter, strong_exit, weak_enter, "
                "weak_exit, default_action, enter, exit"
            )

        normalized_address = normalize_mac(address)
        validated_command = validate_command(command)
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
            event = RawDataEvent(
                timestamp=time.time(),
                mac=frame.mac,
                location=config.location,
                packet=frame.parsed,
                payload=frame.payload,
                device_type=config.type,
            )
            await self.raw_logger.log(event)
            sent_commands = []
            for command in self.estimator.handle(event):
                sent = await self.registry.send_command(command)
                if not sent:
                    self.logger.warning(
                        "No active session for command location=%s command=%s",
                        command.location,
                        command.command,
                    )
                    continue
                sent_commands.append(command)
            self._log_commands(sent_commands)
        elif isinstance(frame.parsed, AlertPacket):
            config = self.config_store.load(frame.mac)
            self.config_store.save(config)
            await self.raw_logger.log_alert(
                AlertEvent(
                    timestamp=time.time(),
                    mac=frame.mac,
                    location=config.location,
                    packet=frame.parsed,
                    payload=frame.payload,
                    device_type=config.type,
                )
            )

    async def _scan_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                async with self.adapter_lock:
                    devices = await discover_named_devices(self.device_name, self.scan_timeout)
                if await self._connected_session_count() == 0:
                    self.logger.info(
                        "BLE scan found %d %s devices",
                        len(devices),
                        self.device_name,
                    )
                for device in devices:
                    await self._start_or_update_session(device)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("BLE scan failed")
            await self._wait_or_stop(self.scan_interval)

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
