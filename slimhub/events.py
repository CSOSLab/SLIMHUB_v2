from __future__ import annotations

from dataclasses import dataclass

from slimhub.config import DEFAULT_DEVICE_TYPE
from slimhub.protocol.nus import AlertPacket, RawDataPacket, ReportPacket


@dataclass(frozen=True)
class RawDataEvent:
    timestamp: float
    mac: str
    location: str
    packet: RawDataPacket
    payload: bytes
    device_type: str = DEFAULT_DEVICE_TYPE
    source_address: str | None = None
    session_id: str | None = None
    receipt_timestamp: float | None = None
    sound_schema_version: str | None = None
    sound_class_count: int | None = None
    sound_semantic_ready: bool | None = None
    sound_profile: str | None = None
    sound_model: str | None = None
    sound_raw_schema: int | None = None
    monotonic_timestamp: float | None = None


@dataclass(frozen=True)
class AlertEvent:
    timestamp: float
    mac: str
    location: str
    packet: AlertPacket
    payload: bytes
    device_type: str = DEFAULT_DEVICE_TYPE


@dataclass(frozen=True)
class ReportEvent:
    timestamp: float
    mac: str
    source_address: str
    location: str
    packet: ReportPacket
    payload: bytes
    device_type: str = DEFAULT_DEVICE_TYPE
    connected: bool | None = None
    session_id: str | None = None
    receipt_timestamp: float | None = None
    clock_offset_ms: float | None = None
    clock_error_ms: float | None = None
    wrap_epoch: int | None = None
    normalized_timestamp: float | None = None
    identity_warning: str | None = None


@dataclass(frozen=True)
class ConnectionStateEvent:
    timestamp: float
    address: str
    connected: bool
    session_id: str | None = None


@dataclass(frozen=True)
class UnitspaceSignalEvent:
    timestamp: float
    mac: str
    location: str
    action: str
    source: str
    boot_id: str | None = None
    primary_seq: int | None = None
    event_seq: int | None = None
    event_id: str | None = None
    event_timestamp_ms: int | None = None
    normalized_timestamp: float | None = None
    confidence: str = "strong"


@dataclass(frozen=True)
class CommandEvent:
    address: str
    command: str
    location: str
    cmd_id: str | None = None
    desired_epoch: int | None = None
    canonical_node_id: str | None = None
    ble_address: str | None = None
    created_at: float | None = None


@dataclass(frozen=True)
class StructuredEvent:
    """An append-only JSONL record for estimator and command lifecycle events."""

    timestamp: float
    kind: str
    mac: str
    data: dict[str, object]
