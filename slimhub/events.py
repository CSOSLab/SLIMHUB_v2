from __future__ import annotations

from dataclasses import dataclass

from slimhub.config import DEFAULT_DEVICE_TYPE
from slimhub.protocol.nus import AlertPacket, RawDataPacket


@dataclass(frozen=True)
class RawDataEvent:
    timestamp: float
    mac: str
    location: str
    packet: RawDataPacket
    payload: bytes
    device_type: str = DEFAULT_DEVICE_TYPE


@dataclass(frozen=True)
class AlertEvent:
    timestamp: float
    mac: str
    location: str
    packet: AlertPacket
    payload: bytes
    device_type: str = DEFAULT_DEVICE_TYPE


@dataclass(frozen=True)
class CommandEvent:
    address: str
    command: str
    location: str
