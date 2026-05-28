from __future__ import annotations

from dataclasses import dataclass

from slimhub.protocol.nus import RawDataPacket


@dataclass(frozen=True)
class RawDataEvent:
    timestamp: float
    mac: str
    location: str
    packet: RawDataPacket
    payload: bytes


@dataclass(frozen=True)
class CommandEvent:
    address: str
    command: str
    location: str
    reason: str
