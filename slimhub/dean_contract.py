from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from slimhub.events import CommandEvent, RawDataEvent, ReportEvent
from slimhub.protocol.nus import normalize_mac


SLIMHUB_CONFIRMED = "slimhub_confirmed"
MAX_OCCUPANCY_SECONDS = 60 * 60
CONFIRM_ACK_TIMEOUT_SECONDS = 2.0
MAX_CONFIRM_ATTEMPTS = 3

LOCATION_PROFILES: dict[str, tuple[str, int]] = {
    "TOILET": ("toilet_v1", 10),
    "KITCHEN": ("kitchen_v1", 9),
    "LIVING": ("living_v1", 5),
    "BEDROOM": ("living_v1", 5),
}


@dataclass
class NodeState:
    mac: str
    bid: str | None = None
    location: str | None = None
    occupancy: str | None = None
    authority: str | None = None
    capture: str | None = None
    config: str | None = None
    semantic: str | None = None
    class_count: int | None = None
    profile: str | None = None
    model: str | None = None
    raw_schema: int | None = None
    source: str | None = None
    timestamp: str | None = None
    commands: str | None = None
    generation: str | None = None
    last_reason: str | None = None
    connected: bool = False
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class InoutCandidate:
    mac: str
    bid: str
    cid: int
    state: str
    location: str
    created_at: float


@dataclass
class PendingConfirmation:
    mac: str
    bid: str
    cid: int
    rid: int
    state: str
    location: str
    created_at: float
    sent_at: float | None = None
    attempts: int = 0
    status: str = "CANDIDATE_RECEIVED"


@dataclass
class OccupantHandoff:
    source_mac: str
    target: InoutCandidate
    created_at: float
    status: str = "HANDOFF_EXIT_SENT"
    exit_sent_at: float | None = None
    exit_attempts: int = 0
    exit_ack_changed: int | None = None
    exit_ack_bid: str | None = None
    exit_ack_cid: int | None = None
    exit_sync_seen: bool = False
    exit_legacy_committed: bool = False


@dataclass(frozen=True)
class ContractRecord:
    timestamp: float
    kind: str
    mac: str
    data: dict[str, object]


class DeanContractStore:
    """MAC state plus the Central-owned home-wide occupancy token."""

    def __init__(
        self,
        state_path: Path | None = None,
        *,
        rid_factory: Callable[[], int] | None = None,
    ) -> None:
        self.state_path = state_path
        self._rid_factory = rid_factory or (lambda: secrets.randbits(32))
        self._nodes: dict[str, NodeState] = {}
        self._pending: dict[
            tuple[str, str, int, str],
            PendingConfirmation,
        ] = {}
        self._used_rids: dict[str, set[int]] = {}
        self._seen_candidates: set[tuple[str, str, int, str]] = set()
        self._seen_results: set[tuple[str, str, int, int, str]] = set()
        self._desired_occupant: str | None = None
        self._confirmed_occupant: str | None = None
        self._last_entered_at: float | None = None
        self._queued_in: InoutCandidate | None = None
        self._handoff: OccupantHandoff | None = None
        self._records: list[ContractRecord] = []
        self._load()

    def handle_connection(self, address: str, connected: bool, timestamp: float) -> None:
        node = self._node(address)
        node.connected = connected
        self._record("node_connection", address, timestamp, connected=connected)
        self._save()

    def handle_raw(self, event: RawDataEvent) -> list[CommandEvent]:
        if event.packet.flag_human_presence != 1:
            return []
        mac = normalize_mac(event.mac)
        timestamp = event.receipt_timestamp or event.timestamp
        detected = event.packet.detected
        self._record(
            "pir_observation",
            mac,
            timestamp,
            detected=detected,
            occupancy_authority=False,
        )
        # The 10/20 RAWDATA packet is transport evidence only. The typed
        # ENTER/EXIT sidecar owns confirmation because it carries bid/cid.
        return []

    def handle_report(self, event: ReportEvent) -> list[CommandEvent]:
        fields = event.packet.fields
        src = fields.get("src", "").strip().upper()
        name = fields.get("event", "").strip().upper()
        if src == "NODE" and name == "STATUS":
            return self._handle_node_status(event)
        if src == "CONFIG" and name in {"STATUS", "APPLIED", "REJECTED"}:
            self._handle_config(event, name)
            return []
        if src == "SOUND" and name == "INFERENCE":
            node = self._node(event.mac)
            node.raw_schema = _int(fields.get("raw"), node.raw_schema)
            self._save()
            return []
        if src != "INOUT":
            return []
        if name in {"ENTER", "EXIT"}:
            return self._handle_candidate(event, name)
        if name in {"CONFIRM_ACK", "CONFIRM_ERROR"}:
            return self._handle_confirm_result(event, name)
        if name == "SEQUENCE":
            return self._handle_sequence(event)
        return []

    def snapshot(self, address: str | None = None) -> object:
        if address is not None:
            node = self._nodes.get(normalize_mac(address))
            return self._node_snapshot(node) if node is not None else {}
        return [
            self._node_snapshot(self._nodes[mac])
            for mac in sorted(self._nodes)
        ]

    def home_snapshot(self) -> dict[str, object]:
        return {
            "desired_occupant": self._desired_occupant,
            "confirmed_occupant": self._confirmed_occupant,
            "last_entered_at": self._last_entered_at,
            "queued_in": self._queued_in.mac if self._queued_in else None,
            "handoff": self._handoff_snapshot(),
            "pending": [
                {
                    "mac": pending.mac,
                    "bid": pending.bid,
                    "cid": pending.cid,
                    "rid": f"{pending.rid:x}",
                    "state": pending.state,
                    "attempts": pending.attempts,
                    "status": pending.status,
                }
                for pending in self._pending.values()
            ],
        }

    def expire_occupancy(self, timestamp: float) -> list[CommandEvent]:
        if (
            self._desired_occupant is None
            or self._last_entered_at is None
            or timestamp - self._last_entered_at < MAX_OCCUPANCY_SECONDS
            or any(pending.state == "out" for pending in self._pending.values())
        ):
            return []
        occupant = self._desired_occupant
        self._record(
            "home_token_timeout",
            occupant,
            timestamp,
            maximum_seconds=MAX_OCCUPANCY_SECONDS,
            command_sent=False,
            reason="confirmation_requires_node_candidate",
        )
        # Do not emit the same diagnostic once per second while waiting for a
        # future Node-owned OUT candidate.
        self._last_entered_at = None
        self._save()
        return []

    def expire_confirmations(self, timestamp: float) -> list[CommandEvent]:
        """Retry ACK-lost transactions with the exact same confirmation identity."""
        commands: list[CommandEvent] = []
        for key, pending in list(self._pending.items()):
            if (
                pending.sent_at is None
                or timestamp - pending.sent_at < CONFIRM_ACK_TIMEOUT_SECONDS
            ):
                continue
            pending.status = "ACK_TIMEOUT"
            if pending.attempts >= MAX_CONFIRM_ATTEMPTS:
                if (
                    self._handoff is not None
                    and self._handoff.target.mac == pending.mac
                    and self._handoff.target.bid == pending.bid
                    and self._handoff.target.cid == pending.cid
                    and self._handoff.target.state == pending.state
                ):
                    self._handoff.status = "RECONCILIATION_REQUIRED"
                self._record(
                    "inout_confirm_timeout",
                    pending.mac,
                    timestamp,
                    bid=pending.bid,
                    cid=pending.cid,
                    rid=f"{pending.rid:x}",
                    state=pending.state,
                    attempts=pending.attempts,
                    terminal=True,
                )
                del self._pending[key]
                continue
            self._record(
                "inout_confirm_retry",
                pending.mac,
                timestamp,
                bid=pending.bid,
                cid=pending.cid,
                rid=f"{pending.rid:x}",
                state=pending.state,
                attempts=pending.attempts,
                same_identity=True,
            )
            # Prevent the one-second maintenance loop from enqueueing another
            # retry before this write attempt reports its transport result.
            pending.sent_at = None
            commands.append(self._command(pending, pending.location))
        handoff = self._handoff
        if (
            handoff is not None
            and handoff.status == "WAIT_A_EXIT_ACK"
            and handoff.exit_sent_at is not None
            and timestamp - handoff.exit_sent_at >= CONFIRM_ACK_TIMEOUT_SECONDS
        ):
            handoff.status = "ACK_TIMEOUT"
            if handoff.exit_attempts >= MAX_CONFIRM_ATTEMPTS:
                handoff.status = "RECONCILIATION_REQUIRED"
                handoff.exit_sent_at = None
                self._record(
                    "inout_handoff_exit_timeout",
                    handoff.source_mac,
                    timestamp,
                    target=handoff.target.mac,
                    attempts=handoff.exit_attempts,
                    terminal=True,
                    occupant_changed=False,
                )
            else:
                self._record(
                    "inout_handoff_exit_retry",
                    handoff.source_mac,
                    timestamp,
                    target=handoff.target.mac,
                    attempts=handoff.exit_attempts,
                    command="exit",
                )
                handoff.exit_sent_at = None
                handoff.status = "HANDOFF_EXIT_SENT"
                commands.append(self._handoff_exit_command(handoff))
        self._save()
        return commands

    def handle_legacy_debug_committed(
        self,
        address: str,
        action: str,
        timestamp: float,
    ) -> list[CommandEvent]:
        """Advance a handoff only after strict legacy DEBUG is on disk."""
        handoff = self._handoff
        mac = normalize_mac(address)
        if (
            handoff is None
            or mac != handoff.source_mac
            or action.strip().upper() != "EXIT"
            or timestamp < handoff.created_at
        ):
            return []
        if not handoff.exit_legacy_committed:
            handoff.exit_legacy_committed = True
            self._record(
                "inout_handoff_exit_legacy_committed",
                mac,
                timestamp,
                target=handoff.target.mac,
            )
        commands = self._advance_handoff(timestamp)
        self._save()
        return commands

    def node_state(self, address: str) -> NodeState:
        return self._node(address)

    def drain_records(self) -> list[ContractRecord]:
        records, self._records = self._records, []
        return records

    def validate_config_change(
        self,
        address: str,
        location: str,
        profile: str | None = None,
    ) -> None:
        normalized_location = location.strip().upper()
        expected = LOCATION_PROFILES.get(normalized_location)
        normalized_profile = profile.strip().lower() if profile is not None else None
        if expected is None or (
            normalized_profile is not None and expected[0] != normalized_profile
        ):
            allowed = ", ".join(
                f"{room}/{values[0]}" for room, values in LOCATION_PROFILES.items()
            )
            raise ValueError(f"unsupported Node configuration; allowed: {allowed}")
        node = self._node(address)
        occupancy = (node.occupancy or "").strip().upper()
        capture = (node.capture or "").strip().upper()
        if occupancy not in {"OUT", "0", "VACANT", "OUTSIDE"}:
            raise ValueError("config_set requires cached Node occupancy OUT")
        if capture != "IDLE":
            raise ValueError("config_set requires cached Node capture IDLE")

    def validate_config_reload(self, address: str) -> None:
        node = self._node(address)
        occupancy = (node.occupancy or "").strip().upper()
        capture = (node.capture or "").strip().upper()
        if occupancy not in {"OUT", "0", "VACANT", "OUTSIDE"}:
            raise ValueError("config_reload requires cached Node occupancy OUT")
        if capture != "IDLE":
            raise ValueError("config_reload requires cached Node capture IDLE")

    def _handle_node_status(self, event: ReportEvent) -> list[CommandEvent]:
        mac = normalize_mac(event.mac)
        fields = dict(event.packet.fields)
        node = self._node(mac)
        old_bid = node.bid
        new_bid = _text(fields.get("bid") or fields.get("boot_id"))
        if old_bid and new_bid and old_bid != new_bid:
            for key, pending in list(self._pending.items()):
                if pending.mac == mac and pending.bid != new_bid:
                    self._record(
                        "inout_confirm_stale",
                        mac,
                        event.timestamp,
                        reason="stale_boot",
                        bid=pending.bid,
                        cid=pending.cid,
                        rid=f"{pending.rid:x}",
                        state=pending.state,
                    )
                    del self._pending[key]
            if (
                self._queued_in is not None
                and self._queued_in.mac == mac
                and self._queued_in.bid != new_bid
            ):
                self._queued_in = None
        node.bid = new_bid or node.bid
        node.location = _text(fields.get("location")) or node.location
        node.occupancy = _text(fields.get("occupancy")) or node.occupancy
        node.authority = _authority(fields.get("authority")) or node.authority
        node.capture = _text(fields.get("capture")) or node.capture
        node.config = _text(fields.get("config")) or node.config
        node.semantic = _text(fields.get("semantic")) or node.semantic
        node.class_count = _int(fields.get("class_count"), node.class_count)
        node.profile = _text(fields.get("profile")) or node.profile
        node.model = _text(fields.get("model")) or node.model
        node.raw_schema = _int(fields.get("raw"), node.raw_schema)
        node.source = _text(fields.get("source")) or node.source
        node.timestamp = _text(fields.get("ts")) or node.timestamp
        node.commands = _text(fields.get("commands")) or node.commands
        node.fields = fields
        occupancy = str(node.occupancy or "").strip().upper()
        if node.authority == SLIMHUB_CONFIRMED:
            if occupancy in {"OUT", "0", "VACANT", "OUTSIDE"}:
                if self._confirmed_occupant == mac:
                    self._confirmed_occupant = None
            elif (
                occupancy in {"IN", "1", "OCCUPIED", "INSIDE"}
                and self._confirmed_occupant is None
            ):
                self._confirmed_occupant = mac
        self._record(
            "node_status",
            mac,
            event.timestamp,
            fields=fields,
            semantic_ready=_semantic_ready(node),
        )
        self._save()
        return []

    def _handle_config(self, event: ReportEvent, name: str) -> None:
        mac = normalize_mac(event.mac)
        fields = dict(event.packet.fields)
        node = self._node(mac)
        if name in {"STATUS", "APPLIED"}:
            node.bid = _text(fields.get("bid") or fields.get("boot_id")) or node.bid
            node.location = _text(fields.get("location")) or node.location
            node.profile = _text(fields.get("profile")) or node.profile
            node.class_count = _int(fields.get("class_count"), node.class_count)
            node.semantic = _text(fields.get("semantic")) or node.semantic
            node.config = (
                _text(fields.get("config") or fields.get("status"))
                or ("READY" if name == "APPLIED" else node.config)
            )
            node.model = _text(fields.get("model")) or node.model
            node.raw_schema = _int(fields.get("raw"), node.raw_schema)
            node.generation = _text(fields.get("generation")) or node.generation
            node.source = _text(fields.get("source")) or node.source
            node.timestamp = _text(fields.get("ts")) or node.timestamp
            if name == "APPLIED":
                node.last_reason = None
            node.fields.update(fields)
        elif name == "REJECTED":
            node.config = "REJECTED"
            node.last_reason = _text(fields.get("reason")) or "config_rejected"
            node.fields.update(fields)
        self._record(
            f"config_{name.lower()}",
            mac,
            event.timestamp,
            fields=fields,
            cached_configuration_changed=name == "APPLIED",
        )
        self._save()

    def _handle_candidate(
        self,
        event: ReportEvent,
        name: str,
    ) -> list[CommandEvent]:
        mac = normalize_mac(event.mac)
        fields = event.packet.fields
        timestamp = event.receipt_timestamp or event.timestamp
        if event.packet.duplicate_fields:
            self._record(
                "inout_candidate_rejected",
                mac,
                timestamp,
                reason="duplicate_fields",
                duplicate_fields=event.packet.duplicate_fields,
            )
            return []
        bid = _candidate_bid(fields.get("boot_id") or fields.get("bid"))
        cid = _candidate_cid(fields.get("event_seq") or fields.get("cid"))
        state = _candidate_state(name, fields)
        if bid is None or cid is None or state is None:
            self._record(
                "inout_candidate_rejected",
                mac,
                timestamp,
                reason="invalid_candidate_identity_or_state",
                fields=dict(fields),
            )
            return []

        key = (mac, bid, cid, state)
        if key in self._seen_candidates:
            self._record(
                "inout_candidate_duplicate",
                mac,
                timestamp,
                bid=bid,
                cid=cid,
                state=state,
            )
            return []
        self._seen_candidates.add(key)
        candidate = InoutCandidate(
            mac=mac,
            bid=bid,
            cid=cid,
            state=state,
            location=event.location,
            created_at=timestamp,
        )
        node = self._node(mac)
        node.bid = bid
        node.location = event.location or node.location
        self._record(
            "inout_candidate",
            mac,
            timestamp,
            bid=bid,
            cid=cid,
            state=state,
            signal=fields.get("signal"),
            code=fields.get("code"),
            radar_state=fields.get("state"),
        )

        if (
            state == "out"
            and self._handoff is not None
            and self._handoff.source_mac == mac
        ):
            self._record(
                "inout_handoff_local_exit_candidate",
                mac,
                timestamp,
                bid=bid,
                cid=cid,
                state=state,
                command_sent=False,
                reason="authoritative_exit_command_already_pending",
            )
            self._save()
            return []

        if state == "in":
            if (
                self._confirmed_occupant is not None
                and self._confirmed_occupant != mac
            ):
                if self._handoff is not None:
                    self._record(
                        "inout_confirm_deferred",
                        mac,
                        timestamp,
                        bid=bid,
                        cid=cid,
                        state=state,
                        reason="handoff_already_in_progress",
                        active_source=self._handoff.source_mac,
                        active_target=self._handoff.target.mac,
                        bounded=True,
                    )
                    self._save()
                    return []
                self._desired_occupant = mac
                self._last_entered_at = timestamp
                self._queued_in = candidate
                self._handoff = OccupantHandoff(
                    source_mac=self._confirmed_occupant,
                    target=candidate,
                    created_at=timestamp,
                )
                self._record(
                    "inout_handoff_exit_pending",
                    mac,
                    timestamp,
                    bid=bid,
                    cid=cid,
                    state=state,
                    source=self._confirmed_occupant,
                    command="exit",
                )
                self._save()
                return [self._handoff_exit_command(self._handoff)]
            self._desired_occupant = mac
            self._last_entered_at = timestamp
            self._queued_in = None

        command = self._new_confirmation(candidate)
        self._save()
        return [command]

    def _handle_sequence(self, event: ReportEvent) -> list[CommandEvent]:
        handoff = self._handoff
        fields = event.packet.fields
        mac = normalize_mac(event.mac)
        result = str(fields.get("result") or "").strip().upper()
        event_id = str(fields.get("event_id") or fields.get("id") or "").strip().upper()
        if (
            handoff is None
            or mac != handoff.source_mac
            or result != "EXIT_SYNC"
            or event_id != "D1"
            or (event.receipt_timestamp or event.timestamp) < handoff.created_at
        ):
            return []
        if not handoff.exit_sync_seen:
            handoff.exit_sync_seen = True
            self._record(
                "inout_handoff_exit_sync",
                mac,
                event.receipt_timestamp or event.timestamp,
                target=handoff.target.mac,
                boot_id=fields.get("boot_id") or fields.get("bid"),
                event_seq=fields.get("event_seq") or fields.get("cid"),
                event_id=event_id,
                result=result,
            )
        commands = self._advance_handoff(event.receipt_timestamp or event.timestamp)
        self._save()
        return commands

    def _handle_handoff_exit_result(
        self,
        event: ReportEvent,
        name: str,
        *,
        bid: str,
        cid: int,
        applied: bool,
        changed: int | None,
        reason: str | None,
        source: str,
    ) -> list[CommandEvent]:
        handoff = self._handoff
        if handoff is None:
            return []
        mac = handoff.source_mac
        if handoff.exit_ack_changed is not None:
            self._record(
                "inout_handoff_exit_ack_duplicate",
                mac,
                event.timestamp,
                target=handoff.target.mac,
                bid=bid,
                cid=cid,
                changed=changed,
            )
            return []
        node = self._node(mac)
        if node.bid is not None and node.bid != bid:
            self._record(
                "inout_handoff_exit_ack_unmatched",
                mac,
                event.timestamp,
                target=handoff.target.mac,
                reason="boot_id_mismatch",
                expected_bid=node.bid,
                reported_bid=bid,
                cid=cid,
            )
            return []
        first_applied = (
            name == "CONFIRM_ACK"
            and applied
            and changed == 1
            and reason == "applied"
            and source == "slimhub"
        )
        already_applied = (
            name == "CONFIRM_ACK"
            and applied
            and changed == 0
            and reason == "already_applied"
            and source == "slimhub"
        )
        if not (first_applied or already_applied):
            node.last_reason = reason or name.lower()
            handoff.status = "RECONCILIATION_REQUIRED"
            self._record(
                "inout_handoff_exit_rejected",
                mac,
                event.timestamp,
                target=handoff.target.mac,
                bid=bid,
                cid=cid,
                applied=applied,
                changed=changed,
                reason=reason,
                event=name,
                terminal=True,
                occupant_changed=False,
            )
            self._save()
            return []

        handoff.exit_ack_changed = changed
        handoff.exit_ack_bid = bid
        handoff.exit_ack_cid = cid
        handoff.exit_sent_at = None
        handoff.status = (
            "WAIT_A_EXIT_LEGACY_COMMIT"
            if first_applied
            else "WAIT_B_ENTER_ACK"
        )
        node.bid = bid
        node.occupancy = "OUT"
        node.last_reason = None
        if self._confirmed_occupant == mac:
            self._confirmed_occupant = None
        self._record(
            "inout_handoff_exit_applied",
            mac,
            event.timestamp,
            target=handoff.target.mac,
            bid=bid,
            cid=cid,
            rid="00000000",
            changed=changed,
            reason=reason,
            legacy=1,
            authoritative=True,
        )
        commands = self._advance_handoff(event.timestamp)
        self._save()
        return commands

    def _advance_handoff(self, timestamp: float) -> list[CommandEvent]:
        handoff = self._handoff
        if handoff is None or handoff.exit_ack_changed is None:
            return []
        if handoff.status == "RECONCILIATION_REQUIRED":
            return []
        if handoff.exit_ack_changed == 1 and not (
            handoff.exit_sync_seen and handoff.exit_legacy_committed
        ):
            handoff.status = "WAIT_A_EXIT_LEGACY_COMMIT"
            return []
        pending_key = (
            handoff.target.mac,
            handoff.target.bid,
            handoff.target.cid,
            handoff.target.state,
        )
        if pending_key in self._pending:
            handoff.status = "WAIT_B_ENTER_ACK"
            return []
        handoff.status = "WAIT_B_ENTER_ACK"
        self._record(
            "inout_handoff_barrier_complete",
            handoff.source_mac,
            timestamp,
            target=handoff.target.mac,
            exit_changed=handoff.exit_ack_changed,
            exit_sync_seen=handoff.exit_sync_seen,
            exit_legacy_committed=handoff.exit_legacy_committed,
        )
        return [self._new_confirmation(handoff.target)]

    def _handle_confirm_result(
        self,
        event: ReportEvent,
        name: str,
    ) -> list[CommandEvent]:
        mac = normalize_mac(event.mac)
        fields = event.packet.fields
        if event.packet.duplicate_fields:
            self._record(
                "inout_confirm_unmatched",
                mac,
                event.timestamp,
                reason="duplicate_fields",
                duplicate_fields=event.packet.duplicate_fields,
            )
            return []
        bid = _candidate_bid(fields.get("bid") or fields.get("boot_id"))
        cid = _candidate_cid(fields.get("cid") or fields.get("event_seq"))
        rid = _hex_int(fields.get("rid"))
        state = _state(fields.get("state"))
        source = str(fields.get("source") or "").strip().lower()
        applied = str(fields.get("applied") or "").strip() == "1"
        changed = _strict_int(fields.get("changed"))
        reason = _text(fields.get("reason"))
        legacy = str(fields.get("legacy") or "").strip()
        if bid is None or cid is None or rid is None or state is None:
            self._record(
                "inout_confirm_unmatched",
                mac,
                event.timestamp,
                reason="missing_correlation_key",
                fields=dict(fields),
            )
            return []
        if (
            self._handoff is not None
            and mac == self._handoff.source_mac
            and rid == 0
            and state == "out"
            and legacy == "1"
        ):
            return self._handle_handoff_exit_result(
                event,
                name,
                bid=bid,
                cid=cid,
                applied=applied,
                changed=changed,
                reason=reason,
                source=source,
            )
        identity = (mac, bid, cid, rid, state)
        if identity in self._seen_results:
            self._record(
                "inout_confirm_duplicate",
                mac,
                event.timestamp,
                bid=bid,
                cid=cid,
                rid=f"{rid:x}",
                state=state,
            )
            return []
        pending_key = (mac, bid, cid, state)
        pending = self._pending.get(pending_key)
        if pending is None or pending.rid != rid:
            if name == "CONFIRM_ERROR" and reason == "request_id_conflict":
                conflicting = next(
                    (
                        (key, item)
                        for key, item in self._pending.items()
                        if item.mac == mac and item.rid == rid
                    ),
                    None,
                )
                if conflicting is not None:
                    conflict_key, conflict = conflicting
                    del self._pending[conflict_key]
                    conflict.status = "CONFIRM_REJECTED"
                    self._record(
                        "inout_confirm_request_id_conflict",
                        mac,
                        event.timestamp,
                        bid=bid,
                        cid=cid,
                        rid=f"{rid:x}",
                        state=state,
                        pending_bid=conflict.bid,
                        pending_cid=conflict.cid,
                        pending_state=conflict.state,
                        terminal=True,
                    )
                    self._save()
                    return []
            self._record(
                "inout_confirm_unmatched",
                mac,
                event.timestamp,
                reason="delayed_or_foreign_response",
                bid=bid,
                cid=cid,
                rid=f"{rid:x}",
                state=state,
            )
            return []
        del self._pending[pending_key]
        self._seen_results.add(identity)
        node = self._node(mac)
        first_applied = (
            name == "CONFIRM_ACK"
            and applied
            and changed == 1
            and source == "slimhub"
            and reason == "applied"
            and legacy == "0"
        )
        already_applied = (
            name == "CONFIRM_ACK"
            and applied
            and changed == 0
            and source == "slimhub"
            and reason == "already_applied"
            and legacy == "0"
        )
        authoritative = first_applied or already_applied
        if authoritative:
            pending.status = (
                "CONFIRMED_CHANGED"
                if first_applied
                else "CONFIRMED_ALREADY_APPLIED"
            )
            node.bid = bid
            node.occupancy = "IN" if state == "in" else "OUT"
            node.last_reason = None
            if state == "in":
                self._confirmed_occupant = mac
            elif self._confirmed_occupant == mac:
                self._confirmed_occupant = None
            self._record(
                "inout_confirm_applied",
                mac,
                event.timestamp,
                bid=bid,
                cid=cid,
                rid=f"{rid:x}",
                state=state,
                changed=changed,
                reason=reason,
                status=pending.status,
                authoritative=True,
            )
            commands: list[CommandEvent] = []
            if (
                state == "in"
                and self._handoff is not None
                and self._handoff.target.mac == mac
                and self._handoff.target.bid == bid
                and self._handoff.target.cid == cid
            ):
                self._record(
                    "inout_handoff_complete",
                    mac,
                    event.timestamp,
                    source=self._handoff.source_mac,
                    target=mac,
                    changed=changed,
                )
                self._handoff = None
                self._queued_in = None
            self._save()
            return commands

        node.last_reason = reason or name.lower()
        pending.status = "CONFIRM_REJECTED"
        if (
            self._handoff is not None
            and self._handoff.target.mac == mac
            and self._handoff.target.bid == bid
            and self._handoff.target.cid == cid
        ):
            self._handoff.status = "RECONCILIATION_REQUIRED"
        self._record(
            "inout_confirm_diagnostic",
            mac,
            event.timestamp,
            bid=bid,
            cid=cid,
            rid=f"{rid:x}",
            state=state,
            reason=reason,
            applied=applied,
            changed=changed,
            legacy=legacy,
            status=pending.status,
            authoritative=False,
            terminal=True,
        )
        self._save()
        return []

    def handle_command_write_result(
        self,
        command: CommandEvent,
        succeeded: bool,
        error: str | None,
        timestamp: float,
    ) -> None:
        if command.command == "exit":
            handoff = self._handoff
            if handoff is None or normalize_mac(command.address) != handoff.source_mac:
                return
            if succeeded:
                handoff.exit_sent_at = timestamp
                handoff.exit_attempts += 1
                handoff.status = "WAIT_A_EXIT_ACK"
                self._record(
                    "inout_handoff_exit_written",
                    handoff.source_mac,
                    timestamp,
                    target=handoff.target.mac,
                    attempts=handoff.exit_attempts,
                )
            else:
                handoff.exit_sent_at = None
                handoff.status = "HANDOFF_EXIT_SENT"
                self._record(
                    "inout_handoff_exit_write_failed",
                    handoff.source_mac,
                    timestamp,
                    target=handoff.target.mac,
                    error=error,
                    retry=True,
                    occupant_changed=False,
                )
            self._save()
            return
        if not command.command.startswith("inout_confirm,"):
            return
        rid = _hex_int(command.cmd_id)
        mac = normalize_mac(command.address)
        for key, pending in list(self._pending.items()):
            if pending.mac == mac and pending.rid == rid:
                if succeeded:
                    pending.sent_at = timestamp
                    pending.attempts += 1
                    pending.status = "CONFIRM_SENT"
                    self._record(
                        "inout_confirm_written",
                        mac,
                        timestamp,
                        bid=pending.bid,
                        cid=pending.cid,
                        rid=f"{pending.rid:x}",
                        state=pending.state,
                        attempts=pending.attempts,
                    )
                    self._save()
                    return
                pending.sent_at = None
                pending.status = "CANDIDATE_RECEIVED"
                self._record(
                    "inout_confirm_write_failed",
                    mac,
                    timestamp,
                    bid=pending.bid,
                    cid=pending.cid,
                    rid=f"{pending.rid:x}",
                    state=pending.state,
                    error=error,
                    retry=True,
                    same_identity=True,
                    status=pending.status,
                )
        self._save()

    def _new_confirmation(
        self,
        candidate: InoutCandidate,
    ) -> CommandEvent:
        rid = self._new_rid(candidate.mac)
        pending = PendingConfirmation(
            mac=candidate.mac,
            bid=candidate.bid,
            cid=candidate.cid,
            rid=rid,
            state=candidate.state,
            location=candidate.location,
            created_at=candidate.created_at,
        )
        self._pending[
            (
                candidate.mac,
                candidate.bid,
                candidate.cid,
                candidate.state,
            )
        ] = pending
        self._record(
            "inout_confirm_pending",
            candidate.mac,
            candidate.created_at,
            bid=candidate.bid,
            cid=candidate.cid,
            rid=f"{rid:x}",
            state=candidate.state,
        )
        return self._command(pending, candidate.location)

    def _new_rid(self, mac: str) -> int:
        used = self._used_rids.setdefault(mac, set())
        for _ in range(64):
            rid = self._rid_factory() & 0xFFFFFFFF
            if rid != 0 and rid not in used:
                used.add(rid)
                return rid
        raise RuntimeError("unable to allocate a unique nonzero Node request id")

    @staticmethod
    def _command(pending: PendingConfirmation, location: str) -> CommandEvent:
        payload = (
            f"inout_confirm,bid={pending.bid},cid={pending.cid},"
            f"state={pending.state},rid={pending.rid:x}"
        )
        return CommandEvent(
            address=pending.mac,
            command=payload,
            location=location,
            cmd_id=f"{pending.rid:x}",
            canonical_node_id=pending.mac,
            created_at=pending.created_at,
        )

    def _handoff_exit_command(self, handoff: OccupantHandoff) -> CommandEvent:
        node = self._node(handoff.source_mac)
        return CommandEvent(
            address=handoff.source_mac,
            command="exit",
            location=node.location or "undefined",
            cmd_id="handoff-exit",
            canonical_node_id=handoff.source_mac,
            created_at=handoff.created_at,
        )

    def _handoff_snapshot(self) -> dict[str, object] | None:
        handoff = self._handoff
        if handoff is None:
            return None
        return {
            "source_mac": handoff.source_mac,
            "target_mac": handoff.target.mac,
            "target_bid": handoff.target.bid,
            "target_cid": handoff.target.cid,
            "status": handoff.status,
            "exit_attempts": handoff.exit_attempts,
            "exit_ack_changed": handoff.exit_ack_changed,
            "exit_sync_seen": handoff.exit_sync_seen,
            "exit_legacy_committed": handoff.exit_legacy_committed,
        }

    def _node(self, address: str) -> NodeState:
        mac = normalize_mac(address)
        return self._nodes.setdefault(mac, NodeState(mac=mac))

    @staticmethod
    def _node_snapshot(node: NodeState) -> dict[str, object]:
        data = asdict(node)
        data["semantic_ready"] = _semantic_ready(node)
        return data

    def _record(self, kind: str, mac: str, timestamp: float, **data: object) -> None:
        self._records.append(
            ContractRecord(timestamp, kind, normalize_mac(mac), dict(data))
        )

    def _save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            mac: self._node_snapshot(node)
            for mac, node in sorted(self._nodes.items())
        }
        payload["_home_token"] = {
            "desired_occupant": self._desired_occupant,
            "confirmed_occupant": self._confirmed_occupant,
            "last_entered_at": self._last_entered_at,
        }
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(document, dict):
            return
        home_token = document.get("_home_token")
        if isinstance(home_token, dict):
            self._desired_occupant = _normalized_mac_or_none(
                home_token.get("desired_occupant")
            )
            self._confirmed_occupant = _normalized_mac_or_none(
                home_token.get("confirmed_occupant")
            )
            self._last_entered_at = _float_or_none(
                home_token.get("last_entered_at")
            )
        allowed = set(NodeState.__dataclass_fields__)
        for raw_mac, value in document.items():
            if raw_mac == "_home_token":
                continue
            if not isinstance(value, dict):
                continue
            try:
                mac = normalize_mac(str(raw_mac))
            except ValueError:
                continue
            data = {key: item for key, item in value.items() if key in allowed}
            data["mac"] = mac
            data["connected"] = False
            try:
                self._nodes[mac] = NodeState(**data)
            except TypeError:
                continue


def build_time_sync_command(now: float | None = None) -> str:
    timestamp = time.time() if now is None else now
    offset = datetime.fromtimestamp(timestamp).astimezone().utcoffset()
    timezone_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    return f"time_sync,epoch_ms={int(timestamp * 1000)},tz_min={timezone_minutes}"


def build_config_set_command(location: str, profile: str | None = None) -> str:
    normalized_location = location.strip().upper()
    expected = LOCATION_PROFILES.get(normalized_location)
    normalized_profile = profile.strip().lower() if profile is not None else None
    if expected is None or (
        normalized_profile is not None and expected[0] != normalized_profile
    ):
        allowed = ", ".join(
            f"{room}/{values[0]}" for room, values in LOCATION_PROFILES.items()
        )
        raise ValueError(f"unsupported Node configuration; allowed: {allowed}")
    payload = f"config_set,location={normalized_location}"
    if normalized_profile is not None:
        payload += f",sound_profile={normalized_profile}"
    return payload


def _semantic_ready(node: NodeState) -> bool:
    return (
        str(node.semantic or "").strip().lower() in {"1", "true", "ready"}
        and str(node.config or "").strip().upper() == "READY"
    )


def _authority(value: object) -> str | None:
    text = str(value or "").strip().lower()
    aliases = {
        "slimhub_confirmed": SLIMHUB_CONFIRMED,
        "slimhub": SLIMHUB_CONFIRMED,
    }
    return aliases.get(text)


def _state(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"in", "inside", "occupied", "1"}:
        return "in"
    if text in {"out", "outside", "vacant", "0"}:
        return "out"
    return None


def _candidate_bid(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if (
        re.fullmatch(r"[0-9a-f]{1,32}", text) is None
        or int(text, 16) == 0
    ):
        return None
    return text


def _candidate_cid(value: object) -> int | None:
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9]{1,10}", text) is None:
        return None
    candidate = int(text, 10)
    return candidate if 0 < candidate <= 0xFFFFFFFF else None


def _candidate_state(name: str, fields: dict[str, str]) -> str | None:
    expected = "in" if name == "ENTER" else "out"
    signal = str(fields.get("signal") or "").strip().lower()
    code = str(fields.get("code") or "").strip()
    if signal and signal != ("enter" if expected == "in" else "exit"):
        return None
    if code and code != ("10" if expected == "in" else "20"):
        return None
    if not signal and not code:
        return None
    return expected


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _int(value: object, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return default


def _strict_int(value: object) -> int | None:
    text = str(value or "").strip()
    if re.fullmatch(r"-?[0-9]+", text) is None:
        return None
    return int(text, 10)


def _hex_int(value: object) -> int | None:
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    try:
        return int(text, 16)
    except ValueError:
        return None


def _normalized_mac_or_none(value: object) -> str | None:
    try:
        return normalize_mac(str(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
