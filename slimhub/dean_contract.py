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
LOCAL_STANDALONE = "local_standalone"
FEEDBACK_WINDOW_SECONDS = 45.0
MAX_NO_PENDING_RETRIES = 1

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
    source: str | None = None
    timestamp: str | None = None
    commands: str | None = None
    generation: str | None = None
    last_reason: str | None = None
    connected: bool = False
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class PendingConfirmation:
    mac: str
    bid: str
    cid: int
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
    """MAC-scoped DEAN Node v2 metadata and confirmation correlation."""

    def __init__(
        self,
        state_path: Path | None = None,
        *,
        rid_factory: Callable[[], int] | None = None,
    ) -> None:
        self.state_path = state_path
        self._rid_factory = rid_factory or (lambda: secrets.randbits(32))
        self._nodes: dict[str, NodeState] = {}
        self._pending: dict[tuple[str, str, int, int], PendingConfirmation] = {}
        self._seen_candidates: set[tuple[str, str, int]] = set()
        self._used_rids: dict[str, set[int]] = {}
        self._raw_candidates: dict[str, tuple[str, float]] = {}
        self._records: list[ContractRecord] = []
        self._load()

    def handle_connection(self, address: str, connected: bool, timestamp: float) -> None:
        node = self._node(address)
        node.connected = connected
        self._record("node_connection", address, timestamp, connected=connected)
        self._save()

    def handle_raw(self, event: RawDataEvent) -> None:
        if event.packet.flag_human_presence != 1 or event.packet.detected not in {10, 20}:
            return
        mac = normalize_mac(event.mac)
        state = "in" if event.packet.detected == 10 else "out"
        timestamp = event.receipt_timestamp or event.timestamp
        self._raw_candidates[mac] = (state, timestamp)
        self._record(
            "inout_raw_candidate",
            mac,
            timestamp,
            state=state,
            confirmable=False,
            reason="rawdata_has_no_bid_or_cid",
        )

    def handle_report(self, event: ReportEvent) -> list[CommandEvent]:
        fields = event.packet.fields
        src = fields.get("src", "").strip().upper()
        name = fields.get("event", "").strip().upper()
        if src == "NODE" and name == "STATUS":
            self._handle_node_status(event)
            return []
        if src == "CONFIG" and name in {"STATUS", "APPLIED", "REJECTED"}:
            self._handle_config(event, name)
            return []
        if src != "INOUT":
            return []
        if name in {"ENTER", "EXIT"}:
            command = self._handle_candidate(event, name)
            return [command] if command is not None else []
        if name in {"CONFIRM_ACK", "CONFIRM_ERROR"}:
            command = self._handle_confirmation(event, name)
            return [command] if command is not None else []
        return []

    def snapshot(self, address: str | None = None) -> object:
        if address is not None:
            node = self._nodes.get(normalize_mac(address))
            return self._node_snapshot(node) if node is not None else {}
        return [
            self._node_snapshot(self._nodes[mac])
            for mac in sorted(self._nodes)
        ]

    def node_state(self, address: str) -> NodeState:
        return self._node(address)

    def drain_records(self) -> list[ContractRecord]:
        records, self._records = self._records, []
        return records

    def validate_config_change(self, address: str, location: str, profile: str) -> None:
        normalized_location = location.strip().upper()
        normalized_profile = profile.strip().lower()
        expected = LOCATION_PROFILES.get(normalized_location)
        if expected is None or expected[0] != normalized_profile:
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

    def _handle_node_status(self, event: ReportEvent) -> None:
        mac = normalize_mac(event.mac)
        fields = dict(event.packet.fields)
        node = self._node(mac)
        old_bid = node.bid
        new_bid = _text(fields.get("bid") or fields.get("boot_id"))
        if old_bid and new_bid and old_bid != new_bid:
            for key, pending in list(self._pending.items()):
                if pending.mac == mac and pending.bid != new_bid:
                    self._record(
                        "inout_confirmation_stale",
                        mac,
                        event.timestamp,
                        reason="stale_boot",
                        bid=pending.bid,
                        cid=pending.cid,
                        rid=f"{pending.rid:x}",
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

    def _handle_config(self, event: ReportEvent, name: str) -> None:
        mac = normalize_mac(event.mac)
        fields = dict(event.packet.fields)
        node = self._node(mac)
        if name == "APPLIED":
            node.bid = _text(fields.get("bid") or fields.get("boot_id")) or node.bid
            node.location = _text(fields.get("location")) or node.location
            node.profile = _text(fields.get("profile")) or node.profile
            node.class_count = _int(fields.get("class_count"), node.class_count)
            node.semantic = _text(fields.get("semantic")) or node.semantic
            node.config = _text(fields.get("status")) or "READY"
            node.model = _text(fields.get("model")) or node.model
            node.generation = _text(fields.get("generation")) or node.generation
            node.source = _text(fields.get("source")) or node.source
            node.timestamp = _text(fields.get("ts")) or node.timestamp
            node.last_reason = None
            node.fields.update(fields)
        elif name == "REJECTED":
            node.config = "REJECTED"
            node.last_reason = _text(fields.get("reason")) or "config_rejected"
            node.fields.update(fields)
        else:
            node.fields.update(fields)
        self._record(
            f"config_{name.lower()}",
            mac,
            event.timestamp,
            fields=fields,
            cached_configuration_changed=name == "APPLIED",
        )
        self._save()

    def _handle_candidate(self, event: ReportEvent, name: str) -> CommandEvent | None:
        mac = normalize_mac(event.mac)
        fields = event.packet.fields
        bid = _text(fields.get("bid") or fields.get("boot_id"))
        cid = _int(fields.get("cid") or fields.get("event_seq"))
        state = "in" if name == "ENTER" else "out"
        if bid is None or cid is None or cid < 0:
            self._record(
                "inout_candidate_rejected",
                mac,
                event.timestamp,
                state=state,
                reason="missing_bid_or_cid",
                fields=dict(fields),
            )
            return None
        candidate_key = (mac, bid, cid)
        if candidate_key in self._seen_candidates:
            self._record(
                "inout_candidate_duplicate",
                mac,
                event.timestamp,
                bid=bid,
                cid=cid,
                state=state,
            )
            return None
        self._seen_candidates.add(candidate_key)

        node = self._node(mac)
        authority = _authority(fields.get("authority")) or node.authority
        if authority == LOCAL_STANDALONE:
            self._record(
                "inout_candidate_observed",
                mac,
                event.timestamp,
                bid=bid,
                cid=cid,
                state=state,
                authority=authority,
                confirmation_suppressed=True,
            )
            return None
        if authority != SLIMHUB_CONFIRMED:
            self._record(
                "inout_candidate_rejected",
                mac,
                event.timestamp,
                bid=bid,
                cid=cid,
                state=state,
                reason="authority_not_slimhub_confirmed",
                authority=authority,
            )
            return None
        if node.bid and node.bid != bid:
            self._record(
                "inout_candidate_rejected",
                mac,
                event.timestamp,
                bid=bid,
                cid=cid,
                state=state,
                reason="stale_boot",
                current_bid=node.bid,
            )
            return None

        pending = self._new_pending(mac, bid, cid, state, event.timestamp)
        raw = self._raw_candidates.get(mac)
        self._record(
            "inout_confirmation_pending",
            mac,
            event.timestamp,
            bid=bid,
            cid=cid,
            rid=f"{pending.rid:x}",
            state=state,
            raw_candidate_correlated=bool(
                raw
                and raw[0] == state
                and abs(event.timestamp - raw[1]) <= FEEDBACK_WINDOW_SECONDS
            ),
        )
        return self._command(pending, event.location)

    def _handle_confirmation(
        self,
        event: ReportEvent,
        name: str,
    ) -> CommandEvent | None:
        mac = normalize_mac(event.mac)
        fields = event.packet.fields
        bid = _text(fields.get("bid") or fields.get("boot_id"))
        cid = _int(fields.get("cid") or fields.get("event_seq"))
        rid = _hex_int(fields.get("rid"))
        state = _state(fields.get("state"))
        source = str(fields.get("source") or "").strip().lower()
        applied = str(fields.get("applied") or "").strip() == "1"
        node = self._node(mac)

        if (
            node.authority == LOCAL_STANDALONE
            and name == "CONFIRM_ACK"
            and source == "local"
            and applied
        ):
            node.occupancy = "IN" if state == "in" else "OUT" if state == "out" else node.occupancy
            self._record(
                "inout_local_result",
                mac,
                event.timestamp,
                fields=dict(fields),
                authoritative=True,
            )
            self._save()
            return None

        if bid is None or cid is None or rid is None:
            self._record(
                "inout_confirmation_unmatched",
                mac,
                event.timestamp,
                reason="missing_correlation_key",
                fields=dict(fields),
            )
            return None
        key = (mac, bid, cid, rid)
        pending = self._pending.get(key)
        if pending is None or (state is not None and state != pending.state):
            self._record(
                "inout_confirmation_unmatched",
                mac,
                event.timestamp,
                reason="delayed_or_foreign_response",
                bid=bid,
                cid=cid,
                rid=f"{rid:x}",
                state=state,
            )
            return None

        del self._pending[key]
        reason = _text(fields.get("reason"))
        authoritative = (
            name == "CONFIRM_ACK"
            and applied
            and source == "slimhub"
            and state == pending.state
        )
        if authoritative:
            node.bid = bid
            node.occupancy = "IN" if pending.state == "in" else "OUT"
            node.last_reason = None
            self._record(
                "inout_confirmation_applied",
                mac,
                event.timestamp,
                bid=bid,
                cid=cid,
                rid=f"{rid:x}",
                state=pending.state,
                authoritative=True,
                fields=dict(fields),
            )
            self._save()
            return None

        node.last_reason = reason or name.lower()
        self._record(
            "inout_confirmation_diagnostic",
            mac,
            event.timestamp,
            bid=bid,
            cid=cid,
            rid=f"{rid:x}",
            state=pending.state,
            reason=reason,
            applied=applied,
            authoritative=False,
            fields=dict(fields),
        )
        if (
            reason == "no_pending_candidate"
            and pending.retries < MAX_NO_PENDING_RETRIES
            and event.timestamp - pending.created_at <= FEEDBACK_WINDOW_SECONDS
        ):
            retry = self._new_pending(
                mac,
                bid,
                cid,
                pending.state,
                event.timestamp,
                retries=pending.retries + 1,
            )
            self._record(
                "inout_confirmation_retry",
                mac,
                event.timestamp,
                bid=bid,
                cid=cid,
                previous_rid=f"{rid:x}",
                rid=f"{retry.rid:x}",
                state=retry.state,
                retry=retry.retries,
            )
            return self._command(retry, event.location)
        self._save()
        return None

    def _new_pending(
        self,
        mac: str,
        bid: str,
        cid: int,
        state: str,
        timestamp: float,
        *,
        retries: int = 0,
    ) -> PendingConfirmation:
        rid = self._new_rid(mac)
        pending = PendingConfirmation(
            mac=mac,
            bid=bid,
            cid=cid,
            rid=rid,
            state=state,
            created_at=timestamp,
            retries=retries,
        )
        self._pending[(mac, bid, cid, rid)] = pending
        return pending

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
        allowed = set(NodeState.__dataclass_fields__)
        for raw_mac, value in document.items():
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


def build_config_set_command(location: str, profile: str) -> str:
    normalized_location = location.strip().upper()
    normalized_profile = profile.strip().lower()
    expected = LOCATION_PROFILES.get(normalized_location)
    if expected is None or expected[0] != normalized_profile:
        allowed = ", ".join(
            f"{room}/{values[0]}" for room, values in LOCATION_PROFILES.items()
        )
        raise ValueError(f"unsupported Node configuration; allowed: {allowed}")
    return f"config_set,location={normalized_location},sound_profile={normalized_profile}"


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
        "local_standalone": LOCAL_STANDALONE,
        "local": LOCAL_STANDALONE,
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
