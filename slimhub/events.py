from __future__ import annotations

from dataclasses import dataclass

from slimhub.protocol.nus import AlertPacket, RawDataPacket


@dataclass(frozen=True)
class RawDataEvent:
    timestamp: float
    mac: str
    location: str
    packet: RawDataPacket
    payload: bytes


@dataclass(frozen=True)
class AlertEvent:
    timestamp: float
    mac: str
    location: str
    packet: AlertPacket
    payload: bytes


@dataclass(frozen=True)
class CommandEvent:
    address: str
    command: str
    location: str
