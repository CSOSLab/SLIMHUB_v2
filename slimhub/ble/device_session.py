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
    VALID_COMMANDS,
    build_command_frame,
    mac_to_bytes,
    normalize_mac,
    parse_frame,
)


FrameHandler = Callable[[str, ParsedFrame], Awaitable[None]]
ConnectionStateHandler = Callable[[str, bool, float], Awaitable[None]]
CommandResultHandler = Callable[[CommandEvent, bool, str | None, float], Awaitable[None]]


class DeviceSession:
    def __init__(
        self,
        target: object | str,
        *,
        on_frame: FrameHandler,
        on_connection_state: ConnectionStateHandler | None = None,
        on_command_result: CommandResultHandler | None = None,
        reconnect_delay: float = 3.0,
        connect_timeout: float = 10.0,
        notify_timeout: float = 5.0,
        adapter_lock: asyncio.Lock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.target = target
        self.address = normalize_mac(str(getattr(target, "address", target)))
        self.name = str(getattr(target, "name", "") or "")
        self.on_frame = on_frame
        self.on_connection_state = on_connection_state
        self.on_command_result = on_command_result
        self.reconnect_delay = reconnect_delay
        self.connect_timeout = connect_timeout
        self.notify_timeout = notify_timeout
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
        self._command_queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending_commands: dict[str, CommandEvent] = {}
        self._command_sequence = 0
        self._command_lock = asyncio.Lock()
        self._frame_handler_lock = asyncio.Lock()
        self._inflight_frame_tasks: set[asyncio.Task[None]] = set()
        self._peak_inflight_frames = 0
        self._negotiated_mtu: int | None = None
        self._max_write_without_response_size: int | None = None
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
        # A disconnected node must converge to its final desired state, not
        # replay every historical enter/exit command after reconnect.
        address_key = normalize_mac(command.canonical_node_id or command.address)
        async with self._command_lock:
            # enter/exit are desired-state commands and may coalesce. Capture
            # start/stop/status commands are ordered operations and must each
            # remain in the single serialized GATT writer queue.
            if command.command in VALID_COMMANDS:
                key = address_key
            else:
                self._command_sequence += 1
                key = f"{address_key}:{self._command_sequence}"
            was_pending = key in self._pending_commands
            self._pending_commands[key] = command
            if not was_pending:
                await self._command_queue.put(key)

    def status(self) -> dict[str, object]:
        return {
            "address": self.address,
            "name": self.name,
            "connected": self.connected,
            "last_seen": self.last_seen,
            "last_error": self.last_error,
            "waiting_for_advertisement": self.waiting_for_advertisement,
            "queued_commands": len(self._pending_commands),
            "inflight_frames": len(self._inflight_frame_tasks),
            "peak_inflight_frames": self._peak_inflight_frames,
            "negotiated_mtu": self._negotiated_mtu,
            "max_write_without_response_size": self._max_write_without_response_size,
        }

    async def _run(self) -> None:
        await self._wait_for_target_update()
        while not self._stop_event.is_set():
            disconnected_event = asyncio.Event()
            command_failure_event = asyncio.Event()
            reported_connected = False
            loop = asyncio.get_running_loop()
            assembler = FrameAssembler()

            def on_disconnect(_: BleakClient) -> None:
                loop.call_soon_threadsafe(disconnected_event.set)

            try:
                client = BleakClient(
                    self.target,
                    disconnected_callback=on_disconnect,
                )
                async with self.adapter_lock:
                    await asyncio.wait_for(
                        client.connect(),
                        timeout=self.connect_timeout,
                    )
                    self._client = client
                    self.connected = bool(client.is_connected)
                    self.waiting_for_advertisement = False

                    await asyncio.wait_for(
                        client.start_notify(
                            NUS_TX_NOTIFY_UUID,
                            self._build_notify_handler(assembler),
                        ),
                        timeout=self.notify_timeout,
                    )
                    self._remember_write_capacity(client)

                    self.last_error = None
                    self.last_seen = time.time()
                    if self.on_connection_state is not None:
                        await self.on_connection_state(self.address, True, self.last_seen)
                        reported_connected = True
                    self._unavailable_logged = False

                command_task = asyncio.create_task(
                    self._command_worker(client, command_failure_event),
                    name=f"ble-command:{self.address}",
                )
                wait_tasks = {
                    asyncio.create_task(self._stop_event.wait()),
                    asyncio.create_task(disconnected_event.wait()),
                    asyncio.create_task(command_failure_event.wait()),
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
                    if not self._unavailable_logged:
                        detail = str(exc).strip() or exc.__class__.__name__
                        self.logger.warning(
                            "%s BLE connection pending: %s",
                            self.address,
                            detail,
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
                if self._inflight_frame_tasks:
                    await asyncio.gather(
                        *list(self._inflight_frame_tasks),
                        return_exceptions=True,
                    )
                assembler.clear()
                self.connected = False
                self._negotiated_mtu = None
                self._max_write_without_response_size = None
                self._client = None
                if reported_connected and self.on_connection_state is not None:
                    await self.on_connection_state(self.address, False, time.time())

            if not self._stop_event.is_set():
                self.waiting_for_advertisement = True
                # A scanner update may arrive first, but reconnecting must not
                # depend on it.  In particular, --no-scan deployments used to
                # remain offline forever after one transient disconnect.
                await self._wait_for_reconnect()

    def _build_notify_handler(self, assembler: FrameAssembler) -> Callable[[object, bytearray], None]:
        expected_mac = mac_to_bytes(self.address)

        def handle_notify(sender: object, data: bytearray) -> None:
            chunk = bytes(data)

            for frame_bytes in assembler.push(chunk):
                if frame_bytes[:6] != expected_mac:
                    self.logger.error(
                        "%s frame rejected before payload parsing: source MAC mismatch",
                        self.address,
                    )
                    continue
                try:
                    frame = parse_frame(frame_bytes)
                except PacketParseError as exc:
                    raw_type = (
                        frame_bytes[6:14].rstrip(b"\x00").decode(
                            "ascii", errors="replace"
                        )
                        if len(frame_bytes) >= 14
                        else "unknown"
                    )
                    declared_length = (
                        int.from_bytes(frame_bytes[14:16], "little")
                        if len(frame_bytes) >= 16
                        else -1
                    )
                    actual_length = max(0, len(frame_bytes) - 18)
                    self.logger.error(
                        "%s frame_type=%s declared_length=%d actual_length=%d "
                        "reject_reason=%s",
                        self.address,
                        raw_type,
                        declared_length,
                        actual_length,
                        exc,
                    )
                    continue
                task = asyncio.create_task(self._dispatch_frame(frame))
                self._inflight_frame_tasks.add(task)
                self._peak_inflight_frames = max(
                    self._peak_inflight_frames,
                    len(self._inflight_frame_tasks),
                )
                if len(self._inflight_frame_tasks) == 64:
                    self.logger.warning(
                        "%s frame processing pressure inflight=%d",
                        self.address,
                        len(self._inflight_frame_tasks),
                    )
                task.add_done_callback(self._frame_task_done)

        return handle_notify

    async def _dispatch_frame(self, frame: ParsedFrame) -> None:
        # Preserve wire order across notification callbacks so state-changing
        # REPORT and RAWDATA records reach the daemon in emission order.
        async with self._frame_handler_lock:
            await self.on_frame(self.address, frame)

    def _frame_task_done(self, task: asyncio.Task[None]) -> None:
        self._inflight_frame_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.logger.error("%s frame handler failed: %s", self.address, error)

    async def _command_worker(
        self,
        client: BleakClient,
        command_failure_event: asyncio.Event | None = None,
    ) -> None:
        while not self._stop_event.is_set():
            key = await self._command_queue.get()
            async with self._command_lock:
                command = self._pending_commands.pop(key, None)
            if command is None:
                continue
            try:
                frame = build_command_frame(command.address, command.command)
                if command.command.startswith("inout_confirm,"):
                    self._validate_confirmation_write(client, frame)
                await client.write_gatt_char(NUS_RX_WRITE_UUID, frame, response=False)
                await self._notify_command_result(command, True, None)
            except Exception as exc:
                self.last_error = str(exc)
                self.logger.warning(
                    "%s command write failed target=%s command=%s error=%s",
                    self.address,
                    command.address,
                    command.command,
                    exc,
                )
                await self._notify_command_result(command, False, str(exc))
                async with self._command_lock:
                    # Preserve a newer desired state if one arrived while the
                    # write was in flight; otherwise retry this idempotent
                    # command after a short backoff.
                    if (
                        not command.command.startswith("inout_confirm,")
                        and key not in self._pending_commands
                    ):
                        self._pending_commands[key] = command
                        await self._command_queue.put(key)
                if command_failure_event is not None:
                    # Continuing to write against a GATT link that has just
                    # failed can strand a capture command indefinitely.  The
                    # outer session loop tears the link down, reconnects, and
                    # drains the preserved command queue on the new client.
                    command_failure_event.set()
                    return
                await asyncio.sleep(min(self.reconnect_delay, 1.0))

    def _remember_write_capacity(self, client: BleakClient) -> None:
        mtu = getattr(client, "mtu_size", None)
        self._negotiated_mtu = (
            int(mtu) if isinstance(mtu, int) and mtu > 0 else None
        )
        self._max_write_without_response_size = self._write_capacity(client)
        self.logger.info(
            "%s BLE MTU=%s max_write_without_response=%s",
            self.address,
            self._negotiated_mtu if self._negotiated_mtu is not None else "unknown",
            (
                self._max_write_without_response_size
                if self._max_write_without_response_size is not None
                else "unknown"
            ),
        )

    def _write_capacity(self, client: BleakClient) -> int | None:
        services = getattr(client, "services", None)
        getter = getattr(services, "get_characteristic", None)
        if callable(getter):
            try:
                characteristic = getter(NUS_RX_WRITE_UUID)
            except (KeyError, RuntimeError):
                characteristic = None
            maximum = getattr(
                characteristic,
                "max_write_without_response_size",
                None,
            )
            if isinstance(maximum, int) and maximum > 0:
                return maximum
        mtu = getattr(client, "mtu_size", None)
        if isinstance(mtu, int) and mtu > 3:
            return mtu - 3
        return None

    def _validate_confirmation_write(
        self,
        client: BleakClient,
        frame: bytes,
    ) -> None:
        capacity = self._write_capacity(client)
        self._max_write_without_response_size = capacity
        if capacity is None:
            raise RuntimeError(
                "cannot verify negotiated ATT write capacity for inout_confirm"
            )
        if len(frame) > capacity:
            raise RuntimeError(
                "inout_confirm frame requires one GATT write of "
                f"{len(frame)} bytes but negotiated capacity is {capacity}"
            )

    async def _notify_command_result(
        self,
        command: CommandEvent,
        succeeded: bool,
        error: str | None,
    ) -> None:
        if self.on_command_result is not None:
            await self.on_command_result(command, succeeded, error, time.time())

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

    async def _wait_for_reconnect(self) -> None:
        """Wait for an explicit target refresh or the normal retry delay."""
        if self._target_updated_event.is_set():
            self._target_updated_event.clear()
            return
        stop_task = asyncio.create_task(self._stop_event.wait())
        update_task = asyncio.create_task(self._target_updated_event.wait())
        delay_task = asyncio.create_task(asyncio.sleep(self.reconnect_delay))
        done, pending = await asyncio.wait(
            {stop_task, update_task, delay_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if update_task in done:
            self._target_updated_event.clear()
        for task in done:
            with suppress(asyncio.CancelledError):
                task.result()

    def _is_expected_connect_failure(self, exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        return isinstance(exc, BleakError)
