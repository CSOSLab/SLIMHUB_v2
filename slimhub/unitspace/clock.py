from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from slimhub.protocol.nus import normalize_mac


UINT32_WRAP = 1 << 32
T = TypeVar("T")


@dataclass(frozen=True)
class NormalizedEventTime:
    timestamp: float
    offset_ms: float | None
    error_ms: float
    wrap_epoch: int
    used_receipt_time: bool


@dataclass
class _ClockState:
    last_uptime_ms: int | None = None
    wrap_epoch: int = 0
    offset_ms: float | None = None
    error_ms: float = 1000.0


class EventReorderBuffer(Generic[T]):
    """Hold a small event-time window and release records in time order."""

    def __init__(self, window_seconds: float = 1.5) -> None:
        self.window_seconds = window_seconds
        self._pending: list[tuple[float, int, T]] = []
        self._latest_timestamp: float | None = None
        self._sequence = 0

    def push(self, item: T, timestamp: float) -> list[T]:
        self._sequence += 1
        self._pending.append((timestamp, self._sequence, item))
        self._latest_timestamp = max(self._latest_timestamp or timestamp, timestamp)
        return self._release_through(self._latest_timestamp - self.window_seconds)

    def flush(self) -> list[T]:
        self._pending.sort(key=lambda item: (item[0], item[1]))
        ready = [item for _, _, item in self._pending]
        self._pending.clear()
        self._latest_timestamp = None
        return ready

    def _release_through(self, cutoff: float) -> list[T]:
        ready = [item for item in self._pending if item[0] <= cutoff]
        self._pending = [item for item in self._pending if item[0] > cutoff]
        ready.sort(key=lambda item: (item[0], item[1]))
        return [item for _, _, item in ready]


class NodeClockNormalizer:
    """Estimate a per-boot monotonic-clock to Central-clock mapping.

    Node uptime is not comparable between nodes.  The first observation of a
    ``(MAC, boot_id)`` stream establishes an offset and later observations use
    a low-pass update.  A missing/malformed uptime deliberately falls back to
    Central receipt time and reports a larger uncertainty.
    """

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _ClockState] = {}

    def normalize(
        self,
        mac: str,
        boot_id: str | None,
        event_ts_ms: int | None,
        receipt_timestamp: float,
    ) -> NormalizedEventTime:
        if boot_id is None or event_ts_ms is None or not 0 <= event_ts_ms < UINT32_WRAP:
            return NormalizedEventTime(
                timestamp=receipt_timestamp,
                offset_ms=None,
                error_ms=1000.0,
                wrap_epoch=0,
                used_receipt_time=True,
            )

        key = (normalize_mac(mac), str(boot_id))
        state = self._states.setdefault(key, _ClockState())
        if state.last_uptime_ms is not None and event_ts_ms < state.last_uptime_ms:
            # A large backwards jump is the uint32 uptime wrap; smaller jumps
            # are reordered packets and retain the same epoch.
            if state.last_uptime_ms - event_ts_ms > UINT32_WRAP // 2:
                state.wrap_epoch += 1
                state.last_uptime_ms = event_ts_ms
        elif state.last_uptime_ms is None or event_ts_ms >= state.last_uptime_ms:
            state.last_uptime_ms = event_ts_ms

        extended_uptime_ms = state.wrap_epoch * UINT32_WRAP + event_ts_ms
        receipt_ms = receipt_timestamp * 1000.0
        sample_offset = receipt_ms - extended_uptime_ms
        if state.offset_ms is None:
            state.offset_ms = sample_offset
            state.error_ms = 0.0
        else:
            residual = abs(sample_offset - state.offset_ms)
            # Keep jitter from one BLE notification from moving old events
            # excessively, while tracking a slowly changing transport delay.
            state.offset_ms = state.offset_ms * 0.9 + sample_offset * 0.1
            state.error_ms = min(1000.0, state.error_ms * 0.9 + residual * 0.1)

        return NormalizedEventTime(
            timestamp=(extended_uptime_ms + state.offset_ms) / 1000.0,
            offset_ms=state.offset_ms,
            error_ms=state.error_ms,
            wrap_epoch=state.wrap_epoch,
            used_receipt_time=False,
        )
