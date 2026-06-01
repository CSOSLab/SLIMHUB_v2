from __future__ import annotations

from dataclasses import dataclass

from slimhub.config import DEFAULT_LOCATION
from slimhub.events import CommandEvent, RawDataEvent
from slimhub.protocol.nus import normalize_mac

ENTER_SIGNALS = {1, 10}
EXIT_SIGNAL = 20
NOISE_THRESHOLD_SECONDS = 5.0


@dataclass
class UnitspaceStatus:
    last_address: str | None = None
    last_location: str | None = None
    last_timestamp: float = 0.0


class SimpleUnitspaceEstimator:
    def __init__(self) -> None:
        self.status = UnitspaceStatus()

    def handle(self, event: RawDataEvent) -> list[CommandEvent]:
        if event.packet.flag_human_presence != 1:
            return []

        address = normalize_mac(event.mac)
        location = event.location or DEFAULT_LOCATION
        signal = event.packet.detected

        if signal in ENTER_SIGNALS:
            return self._handle_enter(address, location, event.timestamp)
        if signal == EXIT_SIGNAL:
            self._remember(address, location, event.timestamp)
            return [CommandEvent(address, "strong_exit", location)]

        return []

    def _handle_enter(
        self,
        address: str,
        location: str,
        timestamp: float,
    ) -> list[CommandEvent]:
        if self.status.last_address is None:
            self._remember(address, location, timestamp)
            return [CommandEvent(address, "strong_enter", location)]

        if address == self.status.last_address:
            if timestamp - self.status.last_timestamp < NOISE_THRESHOLD_SECONDS:
                self._remember(address, location, timestamp)
                return []
            self._remember(address, location, timestamp)
            return []

        previous_address = self.status.last_address
        previous_location = self.status.last_location or DEFAULT_LOCATION
        self._remember(address, location, timestamp)
        return [
            CommandEvent(address, "strong_enter", location),
            CommandEvent(previous_address, "strong_exit", previous_location),
        ]

    def snapshot(self) -> dict[str, object]:
        return {
            "last_address": self.status.last_address,
            "last_location": self.status.last_location,
            "last_timestamp": self.status.last_timestamp,
        }

    def _remember(self, address: str, location: str, timestamp: float) -> None:
        self.status.last_address = address
        self.status.last_location = location
        self.status.last_timestamp = timestamp
