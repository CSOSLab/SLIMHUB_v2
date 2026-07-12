from __future__ import annotations

import logging
import asyncio
from collections.abc import Awaitable, Callable

from slimhub.ble.device_session import (
    CommandResultHandler,
    ConnectionStateHandler,
    DeviceSession,
)
from slimhub.ble.registry import DeviceRegistry
from slimhub.protocol.nus import ParsedFrame, normalize_mac


class BleCentral:
    def __init__(
        self,
        *,
        registry: DeviceRegistry,
        on_frame: Callable[[str, ParsedFrame], Awaitable[None]],
        reconnect_delay: float,
        adapter_lock: asyncio.Lock,
        logger: logging.Logger,
        on_connection_state: ConnectionStateHandler | None = None,
        on_command_result: CommandResultHandler | None = None,
        connect_timeout: float = 10.0,
        notify_timeout: float = 5.0,
    ) -> None:
        self.registry = registry
        self.on_frame = on_frame
        self.on_connection_state = on_connection_state
        self.on_command_result = on_command_result
        self.reconnect_delay = reconnect_delay
        self.connect_timeout = connect_timeout
        self.notify_timeout = notify_timeout
        self.adapter_lock = adapter_lock
        self.logger = logger

    async def ensure_address(self, address: str) -> DeviceSession:
        return await self.ensure_target(normalize_mac(address))

    async def ensure_target(self, target: object | str) -> DeviceSession:
        address = normalize_mac(str(getattr(target, "address", target)))
        session = await self.registry.get(address)
        if session is None:
            session = DeviceSession(
                target,
                on_frame=self.on_frame,
                on_connection_state=self.on_connection_state,
                on_command_result=self.on_command_result,
                reconnect_delay=self.reconnect_delay,
                connect_timeout=self.connect_timeout,
                notify_timeout=self.notify_timeout,
                adapter_lock=self.adapter_lock,
                logger=self.logger,
            )
            await self.registry.add(session)
            session.start()
        else:
            session.update_target(target)
        return session
