from __future__ import annotations

from dataclasses import dataclass

from slimhub.config import normalize_node_location
from slimhub.dean_contract import (
    LOCATION_PROFILES,
    NodeState,
    build_config_set_command,
)
from slimhub.events import CommandEvent, ReportEvent
from slimhub.protocol.nus import normalize_mac


INVALID_CONFIG_STATES = frozenset({"FILE_NOT_FOUND", "CONFIG_INVALID"})
SEMANTIC_DISABLED = frozenset({"0", "FALSE", "DISABLED", "NOT_READY"})


@dataclass
class LocationSyncState:
    mac: str
    desired_location: str | None
    connected: bool = False
    status_seen: bool = False
    config_seen: bool = False
    command_sent: bool = False
    deferred: bool = False
    synchronized: bool = False
    terminal_error: bool = False


@dataclass(frozen=True)
class LocationSyncRecord:
    timestamp: float
    kind: str
    mac: str
    data: dict[str, object]


class LocationSyncCoordinator:
    """MAC-scoped, once-per-connection Central-to-Node location sync FSM."""

    def __init__(self) -> None:
        self._states: dict[str, LocationSyncState] = {}
        self._records: list[LocationSyncRecord] = []

    def handle_connection(
        self,
        address: str,
        connected: bool,
        desired_location: object,
        timestamp: float,
    ) -> None:
        mac = normalize_mac(address)
        desired = normalize_node_location(desired_location)
        if connected:
            self._states[mac] = LocationSyncState(
                mac=mac,
                desired_location=desired,
                connected=True,
            )
            if desired is None:
                self._record(
                    "location_sync_skipped",
                    mac,
                    timestamp,
                    reason="central_location_not_configurable",
                    central_location=str(desired_location or ""),
                )
            return
        state = self._states.get(mac)
        if state is not None:
            state.connected = False

    def handle_report(
        self,
        event: ReportEvent,
        node: NodeState,
        desired_location: object,
    ) -> list[CommandEvent]:
        mac = normalize_mac(event.mac)
        desired = normalize_node_location(desired_location)
        state = self._states.get(mac)
        if state is None:
            # This supports replay/tests while keeping every transaction MAC-scoped.
            state = LocationSyncState(
                mac=mac,
                desired_location=desired,
                connected=bool(event.connected),
            )
            self._states[mac] = state
        elif state.desired_location != desired:
            state.desired_location = desired
            state.command_sent = False
            state.deferred = False
            state.synchronized = False
            state.terminal_error = False

        fields = event.packet.fields
        src = fields.get("src", "").strip().upper()
        name = fields.get("event", "").strip().upper()
        if src == "NODE" and name == "STATUS":
            state.status_seen = True
        elif src == "CONFIG" and name == "STATUS":
            state.config_seen = True
        elif src == "CONFIG" and name == "APPLIED":
            state.config_seen = True
            self._handle_applied(state, node, event)
            return []
        elif src == "CONFIG" and name == "REJECTED":
            state.config_seen = True
            state.terminal_error = True
            state.deferred = False
            self._record(
                "location_sync_rejected",
                mac,
                event.timestamp,
                reason=fields.get("reason") or "config_rejected",
                expected_location=state.desired_location,
                node_location=node.location,
                node_model=node.model,
                node_class_count=node.class_count,
            )
            return []
        elif (
            src in {"COMMAND", "CONFIG"}
            and name in {"ERROR", "COMMAND_ERROR"}
            and state.command_sent
        ):
            state.terminal_error = True
            state.deferred = False
            self._record(
                "location_sync_command_error",
                mac,
                event.timestamp,
                reason=fields.get("reason") or fields.get("error") or "command_error",
                expected_location=state.desired_location,
            )
            return []
        else:
            return []

        command = self._evaluate(state, node, event)
        return [command] if command is not None else []

    def snapshot(self, address: str | None = None) -> object:
        if address is not None:
            state = self._states.get(normalize_mac(address))
            return dict(state.__dict__) if state is not None else {}
        return [
            dict(self._states[mac].__dict__)
            for mac in sorted(self._states)
        ]

    def drain_records(self) -> list[LocationSyncRecord]:
        records, self._records = self._records, []
        return records

    def _evaluate(
        self,
        state: LocationSyncState,
        node: NodeState,
        event: ReportEvent,
    ) -> CommandEvent | None:
        desired = state.desired_location
        if (
            desired is None
            or state.command_sent
            or state.synchronized
            or state.terminal_error
            or not state.status_seen
            or not state.config_seen
        ):
            return None

        node_location = normalize_node_location(node.location)
        semantic = str(node.semantic or "").strip().upper()
        config = str(node.config or "").strip().upper()
        needs_apply = (
            node_location != desired
            or semantic in SEMANTIC_DISABLED
            or config in INVALID_CONFIG_STATES
        )
        if not needs_apply:
            state.synchronized = True
            state.deferred = False
            self._record(
                "location_sync_current",
                state.mac,
                event.timestamp,
                location=desired,
                profile=node.profile,
                class_count=node.class_count,
                model=node.model,
                raw_schema=node.raw_schema,
            )
            return None

        occupancy = str(node.occupancy or "").strip().upper()
        capture = str(node.capture or "").strip().upper()
        if occupancy != "OUT" or capture != "IDLE":
            if not state.deferred:
                self._record(
                    "location_sync_deferred",
                    state.mac,
                    event.timestamp,
                    reason="node_not_out_idle",
                    expected_location=desired,
                    node_location=node.location,
                    occupancy=node.occupancy,
                    capture=node.capture,
                    config=node.config,
                    semantic=node.semantic,
                )
            state.deferred = True
            return None

        state.command_sent = True
        state.deferred = False
        try:
            payload = build_config_set_command(desired)
        except ValueError as exc:
            self._record(
                "location_sync_unsupported",
                state.mac,
                event.timestamp,
                desired_location=desired,
                reason=str(exc),
            )
            return None
        self._record(
            "location_sync_pending",
            state.mac,
            event.timestamp,
            expected_location=desired,
            previous_location=node.location,
            command=payload,
        )
        return CommandEvent(
            address=state.mac,
            command=payload,
            location=desired,
            canonical_node_id=state.mac,
            created_at=event.timestamp,
        )

    def _handle_applied(
        self,
        state: LocationSyncState,
        node: NodeState,
        event: ReportEvent,
    ) -> None:
        desired = state.desired_location
        fields = event.packet.fields
        applied_location = normalize_node_location(fields.get("location") or node.location)
        semantic_ready = (
            str(fields.get("semantic") or node.semantic or "").strip().upper()
            in {"1", "TRUE", "READY"}
        )
        raw_schema = _integer(fields.get("raw"))
        if raw_schema is None:
            raw_schema = node.raw_schema
        if desired is not None and applied_location == desired and semantic_ready:
            applied_class_count = _integer(fields.get("class_count"))
            if applied_class_count is None:
                applied_class_count = node.class_count
            expected_class_count = LOCATION_PROFILES[desired][1]
            if (
                applied_class_count is not None
                and applied_class_count != expected_class_count
            ):
                state.terminal_error = True
                state.command_sent = False
                self._record(
                    "location_sync_applied_mismatch",
                    state.mac,
                    event.timestamp,
                    reason="CONFIG/APPLIED class_count does not match location catalog",
                    expected_location=desired,
                    expected_class_count=expected_class_count,
                    applied_class_count=applied_class_count,
                    model=fields.get("model") or node.model,
                )
                return
            state.synchronized = True
            state.command_sent = False
            state.deferred = False
            self._record(
                "location_sync_applied",
                state.mac,
                event.timestamp,
                location=applied_location,
                profile=fields.get("profile") or node.profile,
                semantic=fields.get("semantic") or node.semantic,
                class_count=applied_class_count,
                model=fields.get("model") or node.model,
                raw_schema=raw_schema,
            )
            if raw_schema != 2:
                self._record(
                    "location_sync_protocol_mismatch",
                    state.mac,
                    event.timestamp,
                    reason="CONFIG/APPLIED did not confirm raw=2",
                    raw_schema=raw_schema,
                )
            return

        state.terminal_error = True
        self._record(
            "location_sync_applied_mismatch",
            state.mac,
            event.timestamp,
            reason="CONFIG/APPLIED does not match Central location or semantic readiness",
            expected_location=desired,
            applied_location=applied_location,
            semantic=fields.get("semantic") or node.semantic,
            raw_schema=raw_schema,
        )

    def _record(
        self,
        kind: str,
        mac: str,
        timestamp: float,
        **data: object,
    ) -> None:
        self._records.append(
            LocationSyncRecord(
                timestamp=timestamp,
                kind=kind,
                mac=normalize_mac(mac),
                data=data,
            )
        )


def _integer(value: object) -> int | None:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return None
