from __future__ import annotations

from dataclasses import dataclass

from slimhub.config import DEFAULT_LOCATION
from slimhub.events import CommandEvent, RawDataEvent
from slimhub.protocol.nus import normalize_mac


@dataclass
class UnitspaceStatus:
    last_address: str | None = None
    last_location: str | None = None
    last_timestamp: float = 0.0


class SimpleUnitspaceEstimator:
    def __init__(self) -> None:
        self.status = UnitspaceStatus()

    def handle(self, event: RawDataEvent) -> list[CommandEvent]:
        if event.packet.detected != 1:
            return []

        address = normalize_mac(event.mac)
        location = event.location or DEFAULT_LOCATION

        if self.status.last_address is None:
            self._remember(address, location, event.timestamp)
            return [
                CommandEvent(address, "strong_enter", location, "first_detection"),
            ]

        if address == self.status.last_address:
            self._remember(address, location, event.timestamp)
            return []

        previous_address = self.status.last_address
        previous_location = self.status.last_location or DEFAULT_LOCATION
        self._remember(address, location, event.timestamp)
        return [
            CommandEvent(address, "strong_enter", location, "movement_detected"),
            CommandEvent(
                previous_address,
                "strong_exit",
                previous_location,
                "movement_detected",
            ),
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
