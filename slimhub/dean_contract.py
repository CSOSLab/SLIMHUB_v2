from __future__ import annotations

import json
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
MAX_STALE_BOOT_RETRIES = 1

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
class PendingSynchronization:
    mac: str
    bid: str
    rid: int
    state: str
    created_at: float
    retries: int = 0


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
        self._pending: dict[tuple[str, str, int, str], PendingSynchronization] = {}
        self._used_rids: dict[str, set[int]] = {}
        self._seen_results: set[tuple[str, str, int, str]] = set()
        self._desired_occupant: str | None = None
        self._confirmed_occupant: str | None = None
        self._last_entered_at: float | None = None
        self._queued_in: tuple[str, str, float] | None = None
        self._awaiting_status: dict[str, tuple[str, float, int]] = {}
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
        if detected != 1:
            return []
        return self._observe_presence(mac, event.location, timestamp)

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
        if name in {"SYNC_ACK", "SYNC_ERROR"}:
            return self._handle_sync_result(event, name)
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
            "queued_in": self._queued_in[0] if self._queued_in else None,
            "pending": [
                {
                    "mac": pending.mac,
                    "bid": pending.bid,
                    "rid": f"{pending.rid:x}",
                    "state": pending.state,
                    "retries": pending.retries,
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
        self._desired_occupant = None
        self._queued_in = None
        self._record(
            "home_token_timeout",
            occupant,
            timestamp,
            maximum_seconds=MAX_OCCUPANCY_SECONDS,
        )
        command = self._new_sync(occupant, "out", timestamp, retries=0)
        self._save()
        return [command] if command is not None else []

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
                        "inout_sync_stale",
                        mac,
                        event.timestamp,
                        reason="stale_boot",
                        bid=pending.bid,
                        rid=f"{pending.rid:x}",
                        state=pending.state,
                    )
                    del self._pending[key]
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
        self._record(
            "node_status",
            mac,
            event.timestamp,
            fields=fields,
            semantic_ready=_semantic_ready(node),
        )
        self._save()
        awaiting = self._awaiting_status.pop(mac, None)
        if awaiting is None or node.bid is None:
            return []
        state, created_at, retries = awaiting
        command = self._new_sync(
            mac,
            state,
            max(event.timestamp, created_at),
            retries=retries,
        )
        return [command] if command is not None else []

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

    def _observe_presence(
        self,
        mac: str,
        location: str,
        timestamp: float,
    ) -> list[CommandEvent]:
        if mac == self._desired_occupant:
            self._record(
                "home_token_observation",
                mac,
                timestamp,
                result="already_desired",
            )
            return []

        previous = self._desired_occupant or self._confirmed_occupant
        awaiting_previous = (
            self._awaiting_status.get(previous)
            if previous is not None
            else None
        )
        if (
            self._confirmed_occupant is None
            and previous is not None
            and awaiting_previous is not None
            and awaiting_previous[0] == "in"
        ):
            del self._awaiting_status[previous]
            self._desired_occupant = mac
            self._last_entered_at = timestamp
            self._record(
                "home_token_target_replaced",
                mac,
                timestamp,
                previous_target=previous,
                previous_sync_not_sent=True,
            )
            command = self._new_sync(mac, "in", timestamp)
            self._save()
            return [command] if command is not None else []

        pending_in = any(
            pending.mac == previous and pending.state == "in"
            for pending in self._pending.values()
        )
        if self._confirmed_occupant is None and pending_in:
            self._desired_occupant = mac
            self._last_entered_at = timestamp
            self._queued_in = (mac, location, timestamp)
            self._record(
                "home_token_target_replaced",
                mac,
                timestamp,
                previous_target=previous,
                previous_in_pending=True,
            )
            self._save()
            return []

        if self._queued_in is not None:
            previous_target = self._queued_in[0]
            self._desired_occupant = mac
            self._last_entered_at = timestamp
            self._queued_in = (mac, location, timestamp)
            self._record(
                "home_token_target_replaced",
                mac,
                timestamp,
                previous_target=previous_target,
                out_transition_unchanged=True,
            )
            self._save()
            return []

        self._desired_occupant = mac
        self._last_entered_at = timestamp
        self._record(
            "home_token_desired",
            mac,
            timestamp,
            previous=previous,
            location=location,
        )
        if previous and previous != mac:
            self._queued_in = (mac, location, timestamp)
            command = self._new_sync(previous, "out", timestamp)
        else:
            self._queued_in = None
            command = self._new_sync(mac, "in", timestamp)
        self._save()
        return [command] if command is not None else []

    def _handle_sync_result(
        self,
        event: ReportEvent,
        name: str,
    ) -> list[CommandEvent]:
        mac = normalize_mac(event.mac)
        fields = event.packet.fields
        bid = _text(fields.get("bid") or fields.get("boot_id"))
        rid = _hex_int(fields.get("rid"))
        state = _state(fields.get("state"))
        source = str(fields.get("source") or "").strip().lower()
        applied = str(fields.get("applied") or "").strip() == "1"
        reason = _text(fields.get("reason"))
        if bid is None or rid is None or state is None:
            self._record(
                "inout_sync_unmatched",
                mac,
                event.timestamp,
                reason="missing_correlation_key",
                fields=dict(fields),
            )
            return []
        key = (mac, bid, rid, state)
        if key in self._seen_results:
            self._record(
                "inout_sync_duplicate",
                mac,
                event.timestamp,
                bid=bid,
                rid=f"{rid:x}",
                state=state,
            )
            return []
        pending = self._pending.pop(key, None)
        if pending is None:
            self._record(
                "inout_sync_unmatched",
                mac,
                event.timestamp,
                reason="delayed_or_foreign_response",
                bid=bid,
                rid=f"{rid:x}",
                state=state,
            )
            return []
        self._seen_results.add(key)
        node = self._node(mac)
        authoritative = name == "SYNC_ACK" and applied and source == "slimhub"
        if authoritative:
            node.bid = bid
            node.occupancy = "IN" if state == "in" else "OUT"
            node.last_reason = None
            changed = str(fields.get("changed") or "").strip() == "1"
            if state == "in":
                self._confirmed_occupant = mac
            elif self._confirmed_occupant == mac:
                self._confirmed_occupant = None
            self._record(
                "inout_sync_applied",
                mac,
                event.timestamp,
                bid=bid,
                rid=f"{rid:x}",
                state=state,
                changed=changed,
                reason=reason,
                authoritative=True,
            )
            commands: list[CommandEvent] = []
            if (
                state == "in"
                and self._queued_in is not None
                and self._queued_in[0] != mac
            ):
                command = self._new_sync(mac, "out", event.timestamp)
                if command is not None:
                    commands.append(command)
            elif state == "out" and self._queued_in is not None:
                target, _, observed_at = self._queued_in
                self._queued_in = None
                command = self._new_sync(target, "in", max(event.timestamp, observed_at))
                if command is not None:
                    commands.append(command)
            self._save()
            return commands

        node.last_reason = reason or name.lower()
        self._record(
            "inout_sync_diagnostic",
            mac,
            event.timestamp,
            bid=bid,
            rid=f"{rid:x}",
            state=state,
            reason=reason,
            applied=applied,
            authoritative=False,
        )
        if (
            name == "SYNC_ERROR"
            and reason == "stale_boot"
            and pending.retries < MAX_STALE_BOOT_RETRIES
        ):
            self._awaiting_status[mac] = (
                state,
                event.timestamp,
                pending.retries + 1,
            )
            self._record(
                "inout_sync_refresh_requested",
                mac,
                event.timestamp,
                previous_bid=bid,
                previous_rid=f"{rid:x}",
                state=state,
            )
            return [
                CommandEvent(
                    address=mac,
                    command="node_status",
                    location=event.location,
                    canonical_node_id=mac,
                    created_at=event.timestamp,
                )
            ]
        self._save()
        return []

    def _new_sync(
        self,
        mac: str,
        state: str,
        timestamp: float,
        *,
        retries: int = 0,
    ) -> CommandEvent | None:
        node = self._node(mac)
        if node.bid is None:
            self._awaiting_status[mac] = (state, timestamp, retries)
            self._record(
                "inout_sync_deferred",
                mac,
                timestamp,
                state=state,
                reason="missing_boot_id",
            )
            return CommandEvent(
                address=mac,
                command="node_status",
                location=node.location or "undefined",
                canonical_node_id=mac,
                created_at=timestamp,
            )
        rid = self._new_rid(mac)
        pending = PendingSynchronization(
            mac=mac,
            bid=node.bid,
            rid=rid,
            state=state,
            created_at=timestamp,
            retries=retries,
        )
        self._pending[(mac, node.bid, rid, state)] = pending
        self._record(
            "inout_sync_pending",
            mac,
            timestamp,
            bid=node.bid,
            rid=f"{rid:x}",
            state=state,
            retry=retries,
        )
        return self._command(pending, node.location or "undefined")

    def _new_rid(self, mac: str) -> int:
        used = self._used_rids.setdefault(mac, set())
        for _ in range(64):
            rid = self._rid_factory() & 0xFFFFFFFF
            if rid != 0 and rid not in used:
                used.add(rid)
                return rid
        raise RuntimeError("unable to allocate a unique nonzero Node request id")

    @staticmethod
    def _command(pending: PendingSynchronization, location: str) -> CommandEvent:
        payload = (
            f"inout_sync,bid={pending.bid},"
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


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _int(value: object, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return default


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
