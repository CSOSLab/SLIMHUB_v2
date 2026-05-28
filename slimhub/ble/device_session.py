from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from bleak import BleakClient
from bleak.exc import BleakError

from slimhub.events import CommandEvent
from slimhub.protocol.nus import (
    NUS_RX_WRITE_UUID,
    NUS_TX_NOTIFY_UUID,
    FrameAssembler,
    PacketParseError,
    ParsedFrame,
    build_command_frame,
    hex_dump,
    normalize_mac,
    parse_frame,
)


FrameHandler = Callable[[str, ParsedFrame], Awaitable[None]]


class DeviceSession:
    def __init__(
        self,
        target: object | str,
        *,
        on_frame: FrameHandler,
        reconnect_delay: float = 3.0,
        adapter_lock: asyncio.Lock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.target = target
        self.address = normalize_mac(str(getattr(target, "address", target)))
        self.name = str(getattr(target, "name", "") or "")
        self.on_frame = on_frame
        self.reconnect_delay = reconnect_delay
        self.adapter_lock = adapter_lock or asyncio.Lock()
        self.logger = logger or logging.getLogger(__name__)

        self.connected = False
        self.last_seen = 0.0
        self.last_error: str | None = None
        self.waiting_for_advertisement = False
        self._unavailable_logged = False

        self._stop_event = asyncio.Event()
        self._target_updated_event = asyncio.Event()
        self._target_updated_event.set()
        self._command_queue: asyncio.Queue[CommandEvent] = asyncio.Queue()
        self._client: BleakClient | None = None
        self._task: asyncio.Task[None] | None = None

    def update_target(self, target: object | str) -> None:
        self.target = target
        self.address = normalize_mac(str(getattr(target, "address", target)))
        self.name = str(getattr(target, "name", "") or self.name)
        self.last_seen = time.time()
        self.waiting_for_advertisement = False
        self._target_updated_event.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name=f"ble:{self.address}")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def send_command(self, command: CommandEvent) -> None:
        await self._command_queue.put(command)

    def status(self) -> dict[str, object]:
        return {
            "address": self.address,
            "name": self.name,
            "connected": self.connected,
            "last_seen": self.last_seen,
            "last_error": self.last_error,
            "waiting_for_advertisement": self.waiting_for_advertisement,
            "queued_commands": self._command_queue.qsize(),
        }

    async def _run(self) -> None:
        await self._wait_for_target_update()
        while not self._stop_event.is_set():
            disconnected_event = asyncio.Event()
            loop = asyncio.get_running_loop()

            def on_disconnect(_: BleakClient) -> None:
                self.logger.warning("%s disconnected", self.address)
                loop.call_soon_threadsafe(disconnected_event.set)

            try:
                if self._unavailable_logged:
                    self.logger.debug("Connecting to %s", self.address)
                else:
                    self.logger.info("Connecting to %s", self.address)
                client = BleakClient(
                    self.target,
                    disconnected_callback=on_disconnect,
                )
                async with self.adapter_lock:
                    await client.connect()
                    self._client = client
                    self.connected = bool(client.is_connected)
                    self.waiting_for_advertisement = False
                    self._unavailable_logged = False

                    assembler = FrameAssembler()
                    await client.start_notify(
                        NUS_TX_NOTIFY_UUID,
                        self._build_notify_handler(assembler),
                    )

                    self.last_error = None
                    self.last_seen = time.time()
                    self.logger.info("%s subscribed to NUS TX", self.address)

                command_task = asyncio.create_task(
                    self._command_worker(client),
                    name=f"ble-command:{self.address}",
                )
                wait_tasks = {
                    asyncio.create_task(self._stop_event.wait()),
                    asyncio.create_task(disconnected_event.wait()),
                }
                done, pending = await asyncio.wait(
                    wait_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    with suppress(asyncio.CancelledError):
                        task.result()

                command_task.cancel()
                await asyncio.gather(command_task, return_exceptions=True)
                with suppress(BleakError, RuntimeError):
                    await client.stop_notify(NUS_TX_NOTIFY_UUID)
                with suppress(BleakError, RuntimeError):
                    await client.disconnect()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                if self._is_expected_connect_failure(exc):
                    if self._unavailable_logged:
                        self.logger.debug(
                            "%s still unavailable; waiting for next advertisement",
                            self.address,
                        )
                    else:
                        self.logger.warning(
                            "%s unavailable; waiting for next advertisement before reconnect: %s",
                            self.address,
                            exc,
                        )
                        self._unavailable_logged = True
                else:
                    self.logger.exception("%s BLE session failed", self.address)
            finally:
                client = self._client
                if client is not None:
                    with suppress(BleakError, RuntimeError, AttributeError):
                        if client.is_connected:
                            await client.disconnect()
                self.connected = False
                self._client = None

            if not self._stop_event.is_set():
                self.waiting_for_advertisement = True
                await self._wait_for_target_update()

    def _build_notify_handler(self, assembler: FrameAssembler) -> Callable[[object, bytearray], None]:
        def handle_notify(sender: object, data: bytearray) -> None:
            chunk = bytes(data)
            self.logger.debug(
                "%s notify sender=%s chunk_len=%d hex=%s",
                self.address,
                sender,
                len(chunk),
                hex_dump(chunk),
            )

            for frame_bytes in assembler.push(chunk):
                try:
                    frame = parse_frame(frame_bytes)
                except PacketParseError as exc:
                    self.logger.error(
                        "%s parse_error=%s frame_hex=%s",
                        self.address,
                        exc,
                        hex_dump(frame_bytes),
                    )
                    continue
                asyncio.create_task(self.on_frame(self.address, frame))

        return handle_notify

    async def _command_worker(self, client: BleakClient) -> None:
        while not self._stop_event.is_set():
            command = await self._command_queue.get()
            try:
                frame = build_command_frame(command.address, command.command)
                await client.write_gatt_char(NUS_RX_WRITE_UUID, frame, response=False)
                self.logger.debug(
                    "command sent location=%s command=%s",
                    command.location,
                    command.command,
                )
            except Exception as exc:
                self.last_error = str(exc)
                self.logger.exception(
                    "%s command failed target=%s command=%s",
                    self.address,
                    command.address,
                    command.command,
                )

    async def _wait_for_target_update(self) -> None:
        while not self._stop_event.is_set():
            if self._target_updated_event.is_set():
                self._target_updated_event.clear()
                return
            stop_task = asyncio.create_task(self._stop_event.wait())
            update_task = asyncio.create_task(self._target_updated_event.wait())
            done, pending = await asyncio.wait(
                {stop_task, update_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                with suppress(asyncio.CancelledError):
                    task.result()

    def _is_expected_connect_failure(self, exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, BleakError):
            message = str(exc).lower()
            return "not found" in message or "not available" in message
        return False
