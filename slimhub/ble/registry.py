from __future__ import annotations

import asyncio

from slimhub.events import CommandEvent
from slimhub.protocol.nus import normalize_mac


class DeviceRegistry:
    def __init__(self) -> None:
        self._sessions = {}
        self._aliases = {}
        self._lock = asyncio.Lock()

    async def add(self, session) -> None:
        async with self._lock:
            self._sessions[normalize_mac(session.address)] = session

    async def get(self, address: str):
        normalized = normalize_mac(address)
        async with self._lock:
            session = self._sessions.get(normalized)
            if session is not None:
                return session
            canonical = self._aliases.get(normalized)
            if canonical is None:
                return None
            return self._sessions.get(canonical)

    async def register_alias(self, alias: str, canonical_address: str) -> None:
        normalized_alias = normalize_mac(alias)
        normalized_canonical = normalize_mac(canonical_address)
        async with self._lock:
            if normalized_canonical in self._sessions:
                self._aliases[normalized_alias] = normalized_canonical

    async def resolve_address(self, address: str) -> str:
        normalized = normalize_mac(address)
        async with self._lock:
            if normalized in self._sessions:
                return normalized
            return self._aliases.get(normalized, normalized)

    async def send_command(self, command: CommandEvent) -> bool:
        session = await self.get(command.address)
        if session is None:
            return False
        await session.send_command(command)
        return True

    async def list_status(self) -> list[dict[str, object]]:
        async with self._lock:
            alias_map: dict[str, list[str]] = {}
            for alias, canonical in self._aliases.items():
                alias_map.setdefault(canonical, []).append(alias)

            statuses = []
            for address, session in self._sessions.items():
                status = session.status()
                status["aliases"] = sorted(alias_map.get(address, []))
                statuses.append(status)
            return statuses

    async def stop_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
        await asyncio.gather(*(session.stop() for session in sessions), return_exceptions=True)
