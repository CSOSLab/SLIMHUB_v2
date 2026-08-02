from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from slimhub.config import DEFAULT_LOCATION
from slimhub.events import CommandEvent, RawDataEvent, ReportEvent, UnitspaceSignalEvent
from slimhub.protocol.nus import ReportPacket, normalize_mac


ENTER_SIGNAL = 10
EXIT_SIGNAL = 20
ENTER_ACTION = "enter"
EXIT_ACTION = "exit"
INOUT_REPORT_SRC = "INOUT"
OCCUPANCY_TIMEOUT_SECONDS = 60 * 60
EXIT_ACK_TIMEOUT_SECONDS = 2.0
MAX_EXIT_ATTEMPTS = 3


@dataclass
class UnitspaceStatus:
    last_address: str | None = None
    last_location: str | None = None
    last_timestamp: float = 0.0
    last_signal_address: str | None = None
    last_signal_action: str | None = None
    last_signal_timestamp: float = 0.0


@dataclass
class PendingExit:
    mac: str
    location: str
    reason: str
    cmd_id: str
    desired_epoch: int
    created_at: float
    attempts: int = 0
    sent_at: float | None = None
    status: str = "EXIT_PENDING"


@dataclass(frozen=True)
class EstimatorRecord:
    kind: str
    mac: str
    timestamp: float
    data: dict[str, object]


class SimpleUnitspaceEstimator:
    """Own the PIR-only active Node while retaining typed-report diagnostics."""

    def __init__(
        self,
        state_path: Path | None = None,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.status = UnitspaceStatus()
        self._state_path = state_path
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._deadline: float | None = None
        self._deadline_epoch: float | None = None
        self._pending_exits: dict[str, PendingExit] = {}
        self._seen_primary: set[tuple[str, str, int]] = set()
        self._seen_sequence: set[tuple[str, str, int]] = set()
        self._desired_epoch = 0
        self._acks: dict[str, dict[str, object]] = {}
        self._confirmed_occupants: set[str] = set()
        self._records: list[EstimatorRecord] = []
        self._load()

    def handle(self, event: RawDataEvent | UnitspaceSignalEvent) -> list[CommandEvent]:
        if not isinstance(event, RawDataEvent):
            address = normalize_mac(event.mac)
            timestamp = event.normalized_timestamp or event.timestamp
            self._record(
                "discard",
                address,
                timestamp,
                reason="typed_signal_is_not_a_pir_command_trigger",
                source=event.source,
            )
            return []

        if event.packet.flag_human_presence != 1:
            return []
        address = normalize_mac(event.mac)
        wall_timestamp = event.receipt_timestamp or event.timestamp
        monotonic_timestamp = (
            event.monotonic_timestamp
            if event.monotonic_timestamp is not None
            else wall_timestamp
        )
        detected = event.packet.detected
        self._record(
            "pir_observation",
            address,
            wall_timestamp,
            detected=detected,
            occupancy_authority=detected in {ENTER_SIGNAL, EXIT_SIGNAL},
        )
        if detected == ENTER_SIGNAL:
            return self._handle_enter(
                address,
                event.location or DEFAULT_LOCATION,
                wall_timestamp,
                monotonic_timestamp,
            )
        if detected == EXIT_SIGNAL:
            self._handle_exit(address, wall_timestamp)
            return []
        self._record(
            "discard",
            address,
            wall_timestamp,
            reason="invalid_pir_signal",
            detected=detected,
        )
        return []

    def handle_report(self, event: ReportEvent) -> list[CommandEvent]:
        """Record typed evidence; only an exact legacy EXIT ACK closes our command."""
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
                command_trigger=False,
            )
            return []

        if event_name in {"CONFIRM_ACK", "CONFIRM_ERROR"}:
            authoritative = self._handle_exit_confirmation(
                address,
                event_name,
                fields,
                timestamp,
            )
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
                changed=fields.get("changed"),
                legacy=fields.get("legacy"),
                reason=fields.get("reason"),
                authoritative=authoritative,
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
            elif result in {"EXIT_CONFIRMED", "EXIT_SYNC"} and event_id == "D1":
                self._confirmed_occupants.discard(address)
                self._record("transition", address, timestamp, result=result, event_id=event_id)
            else:
                self._record("sequence", address, timestamp, result=result, event_id=event_id)
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

    def expire(self, monotonic_timestamp: float) -> list[CommandEvent]:
        commands: list[CommandEvent] = []
        for pending in self._pending_exits.values():
            if pending.status == "RECONCILIATION_REQUIRED":
                continue
            if (
                pending.sent_at is not None
                and monotonic_timestamp - pending.sent_at >= EXIT_ACK_TIMEOUT_SECONDS
            ):
                if pending.attempts >= MAX_EXIT_ATTEMPTS:
                    pending.status = "RECONCILIATION_REQUIRED"
                    self._record(
                        "pir_exit_reconciliation_required",
                        pending.mac,
                        self._wall_clock(),
                        attempts=pending.attempts,
                        reason="exit_ack_timeout",
                    )
                    self._save()
                    continue
                pending.attempts += 1
                pending.sent_at = monotonic_timestamp
                pending.status = "EXIT_RETRY"
                self._record(
                    "pir_exit_retry",
                    pending.mac,
                    self._wall_clock(),
                    attempts=pending.attempts,
                    reason=pending.reason,
                )
                commands.append(self._pending_command(pending))
        if commands:
            self._save()
            return commands

        if (
            self.status.last_address is None
            or self._deadline is None
            or monotonic_timestamp < self._deadline
            or self.status.last_address in self._pending_exits
        ):
            return []
        address = self.status.last_address
        location = self.status.last_location or DEFAULT_LOCATION
        self._deadline = None
        self._deadline_epoch = None
        command = self._start_exit(
            address,
            location,
            "occupancy_expired",
            self._wall_clock(),
            monotonic_timestamp,
        )
        self._save()
        return [command]

    def handle_command_write_result(
        self,
        command: CommandEvent,
        succeeded: bool,
        error: str | None,
        monotonic_timestamp: float,
    ) -> None:
        pending = self._pending_exits.get(normalize_mac(command.address))
        if (
            command.command != EXIT_ACTION
            or pending is None
            or command.cmd_id != pending.cmd_id
        ):
            return
        pending.sent_at = monotonic_timestamp
        pending.status = "WAIT_EXIT_ACK" if succeeded else "EXIT_WRITE_FAILED"
        self._record(
            "pir_exit_write",
            pending.mac,
            self._wall_clock(),
            succeeded=succeeded,
            error=error,
            attempts=pending.attempts,
        )
        self._save()

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
            "active_address": self.status.last_address,
            "expiry_deadline": self._deadline,
            "confirmed_occupants": sorted(self._confirmed_occupants),
            "acks": {address: dict(ack) for address, ack in self._acks.items()},
            "pending_exit": (
                asdict(next(iter(self._pending_exits.values())))
                if len(self._pending_exits) == 1
                else None
            ),
            "pending_exits": {
                mac: asdict(pending)
                for mac, pending in sorted(self._pending_exits.items())
            },
        }

    @staticmethod
    def reorder(events: Iterable[UnitspaceSignalEvent]) -> list[UnitspaceSignalEvent]:
        return sorted(events, key=lambda item: item.normalized_timestamp or item.timestamp)

    def _handle_enter(
        self,
        address: str,
        location: str,
        wall_timestamp: float,
        monotonic_timestamp: float,
    ) -> list[CommandEvent]:
        if address == self.status.last_address:
            self._record(
                "pir_enter_duplicate",
                address,
                wall_timestamp,
                expiry_extended=False,
            )
            return []

        previous_address = self.status.last_address
        previous_location = self.status.last_location or DEFAULT_LOCATION
        self._remember_current(address, location, wall_timestamp)
        self._deadline = monotonic_timestamp + OCCUPANCY_TIMEOUT_SECONDS
        self._deadline_epoch = wall_timestamp + OCCUPANCY_TIMEOUT_SECONDS

        commands: list[CommandEvent] = []
        if previous_address is not None:
            if previous_address in self._pending_exits:
                self._record(
                    "pir_exit_already_pending",
                    previous_address,
                    wall_timestamp,
                    replacement=address,
                )
            else:
                commands.append(
                    self._start_exit(
                        previous_address,
                        previous_location,
                        "cross_node_handoff",
                        wall_timestamp,
                        monotonic_timestamp,
                    )
                )
        self._record(
            "pir_active_changed",
            address,
            wall_timestamp,
            previous=previous_address,
            current=address,
            expiry_seconds=OCCUPANCY_TIMEOUT_SECONDS,
        )
        self._save()
        return commands

    def _handle_exit(self, address: str, timestamp: float) -> None:
        if address in self._pending_exits:
            self._record(
                "pir_exit_authoritative",
                address,
                timestamp,
                source="rawdata_20",
            )
            self._pending_exits.pop(address, None)
        if address == self.status.last_address:
            self._clear_current(timestamp)
            self._deadline = None
            self._deadline_epoch = None
        else:
            self._record(
                "pir_exit_noncurrent",
                address,
                timestamp,
                active=self.status.last_address,
            )
        self._remember_signal(address, EXIT_ACTION, timestamp)
        self._save()

    def _start_exit(
        self,
        address: str,
        location: str,
        reason: str,
        wall_timestamp: float,
        monotonic_timestamp: float,
    ) -> CommandEvent:
        self._desired_epoch += 1
        pending = PendingExit(
            mac=address,
            location=location,
            reason=reason,
            cmd_id=f"pir-exit-{self._desired_epoch}",
            desired_epoch=self._desired_epoch,
            created_at=wall_timestamp,
            attempts=1,
            sent_at=monotonic_timestamp,
            status="EXIT_DISPATCHED",
        )
        self._pending_exits[address] = pending
        self._record(
            "pir_exit_command",
            address,
            wall_timestamp,
            reason=reason,
            attempts=1,
            active_after=self.status.last_address,
        )
        return self._pending_command(pending)

    def _pending_command(self, pending: PendingExit) -> CommandEvent:
        return CommandEvent(
            address=pending.mac,
            command=EXIT_ACTION,
            location=pending.location,
            cmd_id=pending.cmd_id,
            desired_epoch=pending.desired_epoch,
            canonical_node_id=pending.mac,
            created_at=pending.created_at,
        )

    def _handle_exit_confirmation(
        self,
        address: str,
        event_name: str,
        fields: dict[str, str],
        timestamp: float,
    ) -> bool:
        pending = self._pending_exits.get(address)
        if pending is None:
            return False
        if event_name == "CONFIRM_ERROR":
            pending.status = "RECONCILIATION_REQUIRED"
            self._save()
            return False
        changed = _int_or_none(fields.get("changed"))
        reason = fields.get("reason", "").strip().lower()
        authoritative = (
            fields.get("state", "").strip().lower() == "out"
            and fields.get("source", "").strip().lower() == "slimhub"
            and _int_or_none(fields.get("applied")) == 1
            and _int_or_none(fields.get("legacy")) == 1
            and (
                (changed == 1 and reason == "applied")
                or (changed == 0 and reason == "already_applied")
            )
        )
        if not authoritative:
            return False
        self._record(
            "pir_exit_authoritative",
            address,
            timestamp,
            source="legacy_confirm_ack",
            changed=changed,
            reason=reason,
        )
        self._pending_exits.pop(address, None)
        if self.status.last_address == address:
            self._clear_current(timestamp)
            self._deadline = None
            self._deadline_epoch = None
        self._save()
        return True

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

    def _is_primary_replay(
        self,
        address: str,
        boot_id: str | None,
        primary_seq: int | None,
    ) -> bool:
        if boot_id is None or primary_seq is None:
            return False
        key = (address, boot_id, primary_seq)
        if key in self._seen_primary:
            return True
        self._seen_primary.add(key)
        return False

    def _is_sequence_replay(
        self,
        address: str,
        boot_id: str | None,
        event_seq: int | None,
    ) -> bool:
        if boot_id is None or event_seq is None:
            return False
        key = (address, boot_id, event_seq)
        if key in self._seen_sequence:
            return True
        self._seen_sequence.add(key)
        return False

    def _record(self, kind: str, mac: str, timestamp: float, **data: object) -> None:
        self._records.append(EstimatorRecord(kind, normalize_mac(mac), timestamp, dict(data)))

    def _save(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": asdict(self.status),
            "deadline_epoch": self._deadline_epoch,
            "pending_exits": {
                mac: asdict(pending)
                for mac, pending in self._pending_exits.items()
            },
            "desired_epoch": self._desired_epoch,
        }
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._state_path)

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            status = payload.get("status") or {}
            self.status = UnitspaceStatus(**status)
            self._desired_epoch = int(payload.get("desired_epoch") or 0)
            self._deadline_epoch = _float_or_none(payload.get("deadline_epoch"))
            if self._deadline_epoch is not None and self.status.last_address is not None:
                remaining = max(0.0, self._deadline_epoch - self._wall_clock())
                self._deadline = self._monotonic_clock() + remaining
            pending_items = payload.get("pending_exits")
            if not isinstance(pending_items, dict):
                legacy_pending = payload.get("pending_exit")
                pending_items = (
                    {legacy_pending["mac"]: legacy_pending}
                    if isinstance(legacy_pending, dict) and "mac" in legacy_pending
                    else {}
                )
            for pending_data in pending_items.values():
                if not isinstance(pending_data, dict):
                    continue
                pending_data = dict(pending_data)
                pending_data["mac"] = normalize_mac(pending_data["mac"])
                pending_data.setdefault("desired_epoch", self._desired_epoch)
                pending_data["sent_at"] = self._monotonic_clock()
                pending_data["status"] = "EXIT_RESTORED"
                pending = PendingExit(**pending_data)
                self._pending_exits[pending.mac] = pending
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self.status = UnitspaceStatus()
            self._deadline = None
            self._deadline_epoch = None
            self._pending_exits = {}


def inout_report_action(report: ReportPacket) -> str | None:
    if report.fields.get("src", "").strip().upper() != INOUT_REPORT_SRC:
        return None
    return None


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
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
