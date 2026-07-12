from __future__ import annotations

from dataclasses import dataclass

from slimhub.config import DEFAULT_LOCATION
from slimhub.events import CommandEvent, RawDataEvent, UnitspaceSignalEvent
from slimhub.protocol.nus import ReportPacket, normalize_mac

# detected=1 is the legacy PIR-only enter signal; new DEAN_Node_v2 firmware
# uses detected=10 as the primary PIR+RADAR-confirmed enter signal.
LEGACY_PIR_ENTER_SIGNAL = 1
RADAR_CONFIRMED_ENTER_SIGNAL = 10
ENTER_SIGNALS = {LEGACY_PIR_ENTER_SIGNAL, RADAR_CONFIRMED_ENTER_SIGNAL}
EXIT_SIGNAL = 20
NOISE_THRESHOLD_SECONDS = 5.0
ENTER_ACTION = "enter"
EXIT_ACTION = "exit"
INOUT_REPORT_SRC = "INOUT"


@dataclass
class UnitspaceStatus:
    last_address: str | None = None
    last_location: str | None = None
    last_timestamp: float = 0.0
    last_signal_address: str | None = None
    last_signal_action: str | None = None
    last_signal_timestamp: float = 0.0


class SimpleUnitspaceEstimator:
    def __init__(self) -> None:
        self.status = UnitspaceStatus()

    def handle(self, event: RawDataEvent | UnitspaceSignalEvent) -> list[CommandEvent]:
        signal = self._signal_from_event(event)
        if signal is None:
            return []

        address, location, timestamp, action = signal
        if self._is_duplicate_signal(address, action, timestamp):
            if action == ENTER_ACTION and address == self.status.last_address:
                self._remember_current(address, location, timestamp)
            self._remember_signal(address, action, timestamp)
            return []

        self._remember_signal(address, action, timestamp)
        if action == ENTER_ACTION:
            return self._handle_enter(address, location, timestamp)
        return self._handle_exit(address, location, timestamp)

    def _handle_enter(
        self,
        address: str,
        location: str,
        timestamp: float,
    ) -> list[CommandEvent]:
        if self.status.last_address is None:
            self._remember_current(address, location, timestamp)
            return [CommandEvent(address, ENTER_ACTION, location)]

        if address == self.status.last_address:
            self._remember_current(address, location, timestamp)
            return []

        previous_address = self.status.last_address
        previous_location = self.status.last_location or DEFAULT_LOCATION
        self._remember_current(address, location, timestamp)
        return [
            CommandEvent(address, ENTER_ACTION, location),
            CommandEvent(previous_address, EXIT_ACTION, previous_location),
        ]

    def _handle_exit(
        self,
        address: str,
        location: str,
        timestamp: float,
    ) -> list[CommandEvent]:
        if address == self.status.last_address:
            self._clear_current(timestamp)
        return [CommandEvent(address, EXIT_ACTION, location)]

    def snapshot(self) -> dict[str, object]:
        return {
            "last_address": self.status.last_address,
            "last_location": self.status.last_location,
            "last_timestamp": self.status.last_timestamp,
            "last_signal_address": self.status.last_signal_address,
            "last_signal_action": self.status.last_signal_action,
            "last_signal_timestamp": self.status.last_signal_timestamp,
        }

    def _remember_current(self, address: str, location: str, timestamp: float) -> None:
        self.status.last_address = address
        self.status.last_location = location
        self.status.last_timestamp = timestamp

    def _clear_current(self, timestamp: float) -> None:
        self.status.last_address = None
        self.status.last_location = None
        self.status.last_timestamp = timestamp

    def _remember_signal(self, address: str, action: str, timestamp: float) -> None:
        self.status.last_signal_address = address
        self.status.last_signal_action = action
        self.status.last_signal_timestamp = timestamp

    def _is_duplicate_signal(
        self,
        address: str,
        action: str,
        timestamp: float,
    ) -> bool:
        return (
            address == self.status.last_signal_address
            and action == self.status.last_signal_action
            and timestamp - self.status.last_signal_timestamp < NOISE_THRESHOLD_SECONDS
        )

    def _signal_from_event(
        self,
        event: RawDataEvent | UnitspaceSignalEvent,
    ) -> tuple[str, str, float, str] | None:
        address = normalize_mac(event.mac)
        location = event.location or DEFAULT_LOCATION
        if isinstance(event, UnitspaceSignalEvent):
            if event.action not in {ENTER_ACTION, EXIT_ACTION}:
                return None
            return address, location, event.timestamp, event.action

        if event.packet.flag_human_presence != 1:
            return None
        if event.packet.detected in ENTER_SIGNALS:
            return address, location, event.timestamp, ENTER_ACTION
        if event.packet.detected == EXIT_SIGNAL:
            return address, location, event.timestamp, EXIT_ACTION
        return None


def inout_report_action(report: ReportPacket) -> str | None:
    if report.fields.get("src", "").strip().upper() != INOUT_REPORT_SRC:
        return None

    event = report.fields.get("event", "").strip().upper()
    signal = report.fields.get("signal", "").strip().lower()
    code = _int_or_none(report.fields.get("code"))

    has_enter = (
        event == "ENTER"
        or signal == ENTER_ACTION
        or code == RADAR_CONFIRMED_ENTER_SIGNAL
    )
    has_exit = event == "EXIT" or signal == EXIT_ACTION or code == EXIT_SIGNAL
    if has_enter == has_exit:
        return None
    return ENTER_ACTION if has_enter else EXIT_ACTION


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return None
