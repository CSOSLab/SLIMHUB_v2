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
from slimhub.config import AppPaths, DeviceConfigStore
from slimhub.events import AlertEvent, CommandEvent, RawDataEvent
from slimhub.logging import RawDataLogger
from slimhub.protocol.nus import (
    DEFAULT_DEVICE_NAME,
    AlertPacket,
    ParsedFrame,
    RawDataPacket,
    normalize_mac,
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
        self.config_store.save(self.config_store.load(normalized))
        return session.status()

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
        self.config_store.save(self.config_store.load(address))

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
            if command.command == "enter":
                enter_location = location
            elif command.command == "exit":
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
        if command == "config.set":
            config = self.config_store.set_field(
                str(args["address"]),
                str(args["field"]),
                str(args["value"]),
            )
            return self._ok(config.__dict__)
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
                    "connected": False,
                    "last_seen": 0.0,
                    "last_error": None,
                    "queued_commands": 0,
                },
            )
            status["packet_address"] = config.address
            status["configured_name"] = config.name
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
            return list(self.paths.data_dir.glob("*/*/rawdata/*.csv"))
        mac = normalize_mac(address)
        return list(self.paths.data_dir.glob(f"*/{mac}/rawdata/*.csv"))

    async def _wait_or_stop(self, delay_seconds: float) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay_seconds)

    def _ok(self, data: object) -> dict[str, object]:
        return {"ok": True, "data": data, "error": None}
