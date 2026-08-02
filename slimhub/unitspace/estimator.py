from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from slimhub.config import DEFAULT_LOCATION
from slimhub.events import CommandEvent, RawDataEvent, ReportEvent, UnitspaceSignalEvent
from slimhub.protocol.nus import ReportPacket, normalize_mac


LEGACY_PIR_ENTER_SIGNAL = 1
RADAR_CONFIRMED_ENTER_SIGNAL = 10
EXIT_SIGNAL = 20
ENTER_ACTION = "enter"
EXIT_ACTION = "exit"
INOUT_REPORT_SRC = "INOUT"
SIDECAR_COALESCE_SECONDS = 5.0
SIDECAR_FALLBACK_DEDUPE_SECONDS = 1.0


@dataclass
class UnitspaceStatus:
    # ``last_*`` is retained for the CLI/API compatibility, but describes the
    # desired occupant.  ``confirmed_occupants`` is driven only by D0/D1.
    last_address: str | None = None
    last_location: str | None = None
    last_timestamp: float = 0.0
    last_signal_address: str | None = None
    last_signal_action: str | None = None
    last_signal_timestamp: float = 0.0


@dataclass(frozen=True)
class EstimatorRecord:
    kind: str
    mac: str
    timestamp: float
    data: dict[str, object]


class SimpleUnitspaceEstimator:
    """Retain IN/OUT diagnostics without owning Node confirmation state."""

    def __init__(self) -> None:
        self.status = UnitspaceStatus()
        self._last_raw_enter: dict[str, float] = {}
        self._last_sidecar: dict[tuple[str, str], float] = {}
        self._seen_primary: set[tuple[str, str, int]] = set()
        self._seen_sequence: set[tuple[str, str, int]] = set()
        self._desired_epoch = 0
        self._acks: dict[str, dict[str, object]] = {}
        self._confirmed_occupants: set[str] = set()
        self._records: list[EstimatorRecord] = []

    def handle(self, event: RawDataEvent | UnitspaceSignalEvent) -> list[CommandEvent]:
        """Retain PIR/legacy evidence without assigning demo occupancy."""
        if isinstance(event, RawDataEvent):
            address = normalize_mac(event.mac)
            timestamp = event.receipt_timestamp or event.timestamp
            if event.packet.flag_human_presence != 1:
                return []
            self._record(
                "pir_observation",
                address,
                timestamp,
                detected=event.packet.detected,
                occupancy_authority=False,
                discard_reason=(
                    None
                    if event.packet.detected in {0, 1, 10, 20}
                    else "invalid_pir_candidate_code"
                ),
            )
            return []

        address = normalize_mac(event.mac)
        timestamp = event.normalized_timestamp or event.timestamp
        self._record(
            "discard",
            address,
            timestamp,
            reason="demo_has_no_local_movement_candidates",
            source=event.source,
        )
        return []

    def handle_report(self, event: ReportEvent) -> list[CommandEvent]:
        """Record ACK/confirmation reports and handle ENTER sidecars only."""
        report = event.packet
        fields = report.fields
        if fields.get("src", "").strip().upper() != INOUT_REPORT_SRC:
            return []

        event_name = fields.get("event", "").strip().upper()
        address = normalize_mac(event.mac)
        timestamp = event.normalized_timestamp or event.receipt_timestamp or event.timestamp
        boot_id = _optional_text(fields.get("boot_id"))
        primary_seq = _int_or_none(fields.get("primary_seq"))
        event_seq = _int_or_none(fields.get("event_seq"))
        event_id = _optional_text(fields.get("event_id") or fields.get("id"))
        node_timestamp = _int_or_none(fields.get("event_ts_ms"))

        if event_name in {"ENTER", "EXIT"}:
            self._record(
                "candidate_evidence",
                address,
                timestamp,
                action=event_name.lower(),
                boot_id=boot_id,
                event_seq=event_seq,
                signal=fields.get("signal"),
                code=fields.get("code"),
                radar_state=fields.get("state"),
            )
            return []

        if event_name in {"CONFIRM_ACK", "CONFIRM_ERROR"}:
            self._record(
                "confirmation_evidence",
                address,
                timestamp,
                event=event_name,
                bid=fields.get("bid"),
                cid=fields.get("cid"),
                rid=fields.get("rid"),
                state=fields.get("state"),
                applied=fields.get("applied"),
                reason=fields.get("reason"),
            )
            return []

        if event_name == "EVENT":
            if self._is_primary_replay(address, boot_id, primary_seq):
                self._record("discard", address, timestamp, reason="primary_replay")
                return []
            if event_id in {"C0", "C1"}:
                action = ENTER_ACTION if event_id == "C0" else EXIT_ACTION
                self._acks[address] = {
                    "event_id": event_id,
                    "action": action,
                    "cmd_id": _optional_text(fields.get("cmd_id")),
                    "applied_state": fields.get("occupied") or fields.get("applied_state"),
                    "target_match": fields.get("target_match"),
                    "source_count": fields.get("source_count"),
                    "ack_timestamp": timestamp,
                }
                self._record("ack", address, timestamp, **self._acks[address])
            return []

        if event_name == "SEQUENCE":
            if self._is_sequence_replay(address, boot_id, event_seq):
                self._record("discard", address, timestamp, reason="sequence_replay")
                return []
            result = fields.get("result", "").strip().upper()
            if result == "ENTER_CONFIRMED" and event_id == "D0":
                self._confirmed_occupants.add(address)
                self._record("transition", address, timestamp, result=result, event_id=event_id)
            elif result == "EXIT_CONFIRMED" and event_id == "D1":
                self._confirmed_occupants.discard(address)
                self._record("transition", address, timestamp, result=result, event_id=event_id)
            else:
                self._record("sequence", address, timestamp, result=result, event_id=event_id)
            # Never feed SEQUENCE reports into candidate inference.
            return []

        if event_name == "STATE" and "occupied" in fields:
            occupied = _bool_or_none(fields.get("occupied"))
            if occupied is not None:
                if occupied:
                    self._confirmed_occupants.add(address)
                else:
                    self._confirmed_occupants.discard(address)
                self._record("reconciliation", address, timestamp, occupied=occupied)
            return []

        return []

    def drain_records(self) -> list[EstimatorRecord]:
        records, self._records = self._records, []
        return records

    def snapshot(self) -> dict[str, object]:
        return {
            "last_address": self.status.last_address,
            "last_location": self.status.last_location,
            "last_timestamp": self.status.last_timestamp,
            "last_signal_address": self.status.last_signal_address,
            "last_signal_action": self.status.last_signal_action,
            "last_signal_timestamp": self.status.last_signal_timestamp,
            "desired_address": self.status.last_address,
            "confirmed_occupants": sorted(self._confirmed_occupants),
            "acks": {address: dict(ack) for address, ack in self._acks.items()},
        }

    @staticmethod
    def reorder(events: Iterable[UnitspaceSignalEvent]) -> list[UnitspaceSignalEvent]:
        """Stable utility for queued/replayed reports after clock normalization."""
        return sorted(events, key=lambda item: item.normalized_timestamp or item.timestamp)

    def _handle_enter(self, address: str, location: str, timestamp: float) -> list[CommandEvent]:
        location = location or DEFAULT_LOCATION
        if self.status.last_address is None:
            before = self.status.last_address
            self._remember_current(address, location, timestamp)
            return [self._command(address, ENTER_ACTION, location, timestamp, before=before)]
        if address == self.status.last_address:
            self._remember_current(address, location, timestamp)
            self._record("candidate", address, timestamp, result="same_desired_node")
            return []

        previous_address = self.status.last_address
        previous_location = self.status.last_location or DEFAULT_LOCATION
        self._remember_current(address, location, timestamp)
        return [
            self._command(address, ENTER_ACTION, location, timestamp, before=previous_address),
            self._command(previous_address, EXIT_ACTION, previous_location, timestamp, before=previous_address),
        ]

    def _handle_legacy_exit(self, address: str, location: str, timestamp: float) -> list[CommandEvent]:
        self._remember_signal(address, EXIT_ACTION, timestamp)
        if address != self.status.last_address:
            self._record("discard", address, timestamp, reason="exit_not_desired_occupant")
            return []
        before = self.status.last_address
        self._clear_current(timestamp)
        return [
            self._command(
                address,
                EXIT_ACTION,
                location or DEFAULT_LOCATION,
                timestamp,
                before=before,
            )
        ]

    def _command(
        self,
        address: str,
        action: str,
        location: str,
        timestamp: float,
        *,
        before: str | None,
    ) -> CommandEvent:
        self._desired_epoch += 1
        command = CommandEvent(
            address=address,
            command=action,
            location=location,
            cmd_id=f"{address.replace(':', '')}-{self._desired_epoch}",
            desired_epoch=self._desired_epoch,
            canonical_node_id=address,
            created_at=timestamp,
        )
        self._record(
            "command",
            address,
            timestamp,
            command=action,
            cmd_id=command.cmd_id,
            desired_epoch=command.desired_epoch,
            estimator_before=before,
            estimator_after=self.status.last_address,
        )
        return command

    def _remember_current(self, address: str, location: str, timestamp: float) -> None:
        self.status.last_address = address
        self.status.last_location = location or DEFAULT_LOCATION
        self.status.last_timestamp = timestamp
        self._remember_signal(address, ENTER_ACTION, timestamp)

    def _clear_current(self, timestamp: float) -> None:
        self.status.last_address = None
        self.status.last_location = None
        self.status.last_timestamp = timestamp

    def _remember_signal(self, address: str, action: str, timestamp: float) -> None:
        self.status.last_signal_address = address
        self.status.last_signal_action = action
        self.status.last_signal_timestamp = timestamp

    def _is_exact_replay(self, event: UnitspaceSignalEvent) -> bool:
        address = normalize_mac(event.mac)
        if event.source == "PRIMARY" and event.boot_id and event.primary_seq is not None:
            return self._is_primary_replay(address, event.boot_id, event.primary_seq)
        if event.boot_id and event.event_seq is not None:
            return self._is_sequence_replay(address, event.boot_id, event.event_seq)
        return False

    def _is_primary_replay(self, address: str, boot_id: str | None, primary_seq: int | None) -> bool:
        if boot_id is None or primary_seq is None:
            return False
        key = (address, boot_id, primary_seq)
        if key in self._seen_primary:
            return True
        self._seen_primary.add(key)
        return False

    def _is_sequence_replay(self, address: str, boot_id: str | None, event_seq: int | None) -> bool:
        if boot_id is None or event_seq is None:
            return False
        key = (address, boot_id, event_seq)
        if key in self._seen_sequence:
            return True
        self._seen_sequence.add(key)
        return False

    def _record(self, kind: str, mac: str, timestamp: float, **data: object) -> None:
        self._records.append(EstimatorRecord(kind, normalize_mac(mac), timestamp, dict(data)))


def inout_report_action(report: ReportPacket) -> str | None:
    if report.fields.get("src", "").strip().upper() != INOUT_REPORT_SRC:
        return None
    return None


def _is_preliminary_enter_report(report: ReportPacket) -> bool:
    fields = report.fields
    if fields.get("src", "").strip().upper() != INOUT_REPORT_SRC:
        return False
    return (
        fields.get("event", "").strip().upper() == "ENTER"
        and (
            fields.get("signal", "").strip().lower() == ENTER_ACTION
            or _int_or_none(fields.get("code")) == RADAR_CONFIRMED_ENTER_SIGNAL
        )
    )


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return None


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _bool_or_none(value: object) -> bool | None:
    text = str(value).strip().lower() if value is not None else ""
    if text in {"1", "true", "yes", "inside", "occupied"}:
        return True
    if text in {"0", "false", "no", "outside", "vacant"}:
        return False
    return None
