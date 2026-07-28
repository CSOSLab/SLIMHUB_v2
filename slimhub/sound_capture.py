from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from slimhub.events import ReportEvent
from slimhub.protocol.nus import SOUND_STOP_COMMAND, normalize_mac, validate_sound_label


CAPTURE_ID_PATTERN = re.compile(r"^[0-9A-Fa-f]{1,8}$")
SUCCESS_EVENT = "CAPTURE_DONE"
FAILURE_EVENTS = {"CAPTURE_CANCELLED", "CAPTURE_ERROR"}
TERMINAL_EVENTS = {SUCCESS_EVENT, *FAILURE_EVENTS}
ACCEPT_EVENTS = {"CAPTURE_ARMED"}
LIFECYCLE_EVENTS = {
    "CAPTURE_ARMED",
    "CAPTURE_START",
    "CAPTURE_STATUS",
    "CAPTURE_SEGMENT",
    "CAPTURE_STORAGE_ERROR",
    *TERMINAL_EVENTS,
}


@dataclass
class SoundCommandIntent:
    request_id: int
    address: str
    payload: str
    label: str
    threshold_rms: int
    max_seconds: int
    silence_seconds: int
    created_at: float
    session_key: tuple[str, int] | None = None
    command_errors: list[str] = field(default_factory=list)
    transport_errors: list[str] = field(default_factory=list)
    change_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass
class SoundCaptureSession:
    node_mac: str
    ble_address: str
    capture_id: int
    label: str
    location: str
    request_id: int | None
    state: str = "ARMED"
    accepted: bool = False
    terminal_event: str | None = None
    complete: bool | None = None
    reason: str | None = None
    segment: int | None = None
    samples: int | None = None
    blocks: int | None = None
    segment_samples: int | None = None
    segment_blocks: int | None = None
    queue_drop: int = 0
    ble_drop: int = 0
    started_at: float | None = None
    updated_at: float | None = None
    ended_at: float | None = None
    connected: bool = True
    storage_errors: list[dict[str, object]] = field(default_factory=list)
    reports: dict[str, dict[str, object]] = field(default_factory=dict)
    seen_reports: set[tuple[str, str, str, str]] = field(default_factory=set, repr=False)
    revision: int = 0

    @property
    def cid(self) -> str:
        return f"{self.capture_id:08x}"

    @property
    def terminal(self) -> bool:
        return self.terminal_event in TERMINAL_EVENTS

    @property
    def success(self) -> bool:
        return self.terminal_event == SUCCESS_EVENT and self.complete is True


class SoundCaptureStore:
    """Track Node-uSD sound capture reports and satisfy CLI waiters.

    BLE carries commands and CSV REPORT metadata only. AUDIO/WAVFILE frames are
    intentionally ignored by the protocol layer and this store never creates
    Central audio files or manifests.
    """

    def __init__(self, _paths: object = None, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self._request_sequence = 0
        self._intents: dict[int, SoundCommandIntent] = {}
        self._pending_by_address: dict[str, list[int]] = {}
        self._sessions: dict[tuple[str, int], SoundCaptureSession] = {}
        self._latest_status: dict[str, dict[str, object]] = {}
        self._status_revisions: dict[str, int] = {}
        self._status_events: dict[str, asyncio.Event] = {}
        self._firmware_status: dict[str, dict[str, str]] = {}

    def register_command(self, address: str, command: str, timestamp: float) -> int | None:
        normalized = normalize_mac(address)
        if command.startswith("sound_start,"):
            fields = _command_fields(command)
            label = validate_sound_label(fields["label"])
            threshold_rms = int(fields["thr"])
            max_seconds = int(fields["max"])
            silence_seconds = int(fields["silence"])
        elif command.startswith("sound_bg,"):
            fields = _command_fields(command)
            label = "background"
            threshold_rms = 0
            max_seconds = int(fields["max"])
            silence_seconds = 0
        elif command == SOUND_STOP_COMMAND:
            for session in self._sessions_for_address(normalized):
                if not session.terminal:
                    session.state = "STOP_REQUESTED"
                    session.updated_at = timestamp
                    self._touch_session(session)
            return None
        else:
            return None

        self._request_sequence += 1
        intent = SoundCommandIntent(
            request_id=self._request_sequence,
            address=normalized,
            payload=command,
            label=label,
            threshold_rms=threshold_rms,
            max_seconds=max_seconds,
            silence_seconds=silence_seconds,
            created_at=timestamp,
        )
        self._intents[intent.request_id] = intent
        self._pending_by_address.setdefault(normalized, []).append(intent.request_id)
        return intent.request_id

    def handle_report(self, event: ReportEvent) -> None:
        fields = event.packet.fields
        if fields.get("src", "").upper() != "SOUND":
            return
        node_mac = normalize_mac(event.mac)
        ble_address = normalize_mac(event.source_address)
        event_name = fields.get("event", "").upper()
        self._remember_latest_status(node_mac, ble_address, event_name, event)

        if event_name in {"COMMAND_ERROR", "CAPTURE_BUSY"}:
            intent = self._pending_intent(node_mac, ble_address)
            if intent is not None:
                intent.command_errors.append(fields.get("reason") or event_name.lower())
                intent.change_event.set()
            return

        capture_id = _capture_id(fields.get("cid"))
        if capture_id is None or event_name not in LIFECYCLE_EVENTS:
            return

        key = (node_mac, capture_id)
        session = self._sessions.get(key)
        if session is None:
            session = SoundCaptureSession(
                node_mac=node_mac,
                ble_address=ble_address,
                capture_id=capture_id,
                label=_report_label(fields.get("label")),
                location=event.location,
                request_id=None,
                connected=bool(event.connected) if event.connected is not None else True,
            )
            self._sessions[key] = session

        # A cid belongs to a new CLI request only after the Node explicitly
        # accepts that command. This prevents a cached terminal REPORT from an
        # older capture being mistaken for completion of a newer request.
        if (
            event_name == "CAPTURE_ARMED"
            and session.request_id is None
            and not session.terminal
        ):
            intent = self._pending_intent(node_mac, ble_address)
            if intent is not None:
                session.request_id = intent.request_id
                session.label = intent.label
                intent.session_key = key
                self._remove_pending(intent)

        dedupe_key = (
            event_name,
            "" if event_name in TERMINAL_EVENTS else fields.get("segment", ""),
            "" if event_name in TERMINAL_EVENTS else fields.get("state", ""),
            "",
        )
        if dedupe_key in session.seen_reports:
            return
        session.seen_reports.add(dedupe_key)
        session.reports[event_name] = {
            "timestamp": event.timestamp,
            "message": event.packet.message,
            "fields": dict(fields),
        }
        session.updated_at = event.timestamp
        session.connected = bool(event.connected) if event.connected is not None else True
        session.segment = _optional_int(fields.get("segment"), session.segment)

        # Terminal state is monotonic. Delayed lifecycle notifications can
        # still be retained as diagnostics, but they cannot reopen a capture.
        if session.terminal:
            self._touch_session(session)
            return

        if event_name == "CAPTURE_ARMED":
            session.accepted = True
            if session.started_at is None:
                session.state = "ARMED"
        elif event_name == "CAPTURE_START":
            session.state = "ACTIVE"
            session.started_at = session.started_at or event.timestamp
        elif event_name == "CAPTURE_STATUS":
            state = fields.get("state", "").upper()
            if state:
                session.state = state
        elif event_name == "CAPTURE_SEGMENT":
            session.segment_samples = _optional_int(
                fields.get("sample_count"),
                session.segment_samples,
            )
            session.state = "ACTIVE"
        elif event_name == "CAPTURE_STORAGE_ERROR":
            session.storage_errors.append(
                {
                    "timestamp": event.timestamp,
                    "reason": fields.get("reason") or "sd_write_error",
                    "report": event.packet.message,
                }
            )
        elif event_name in TERMINAL_EVENTS:
            session.terminal_event = event_name
            session.complete = _complete_flag(fields.get("complete"))
            session.reason = fields.get("reason") or event_name.lower()
            session.samples = _optional_int(fields.get("samples"), session.samples)
            session.blocks = _optional_int(fields.get("blocks"), session.blocks)
            session.segment_samples = _optional_int(
                fields.get("segment_samples"),
                session.segment_samples,
            )
            session.segment_blocks = _optional_int(
                fields.get("segment_blocks"),
                session.segment_blocks,
            )
            session.queue_drop = _optional_int(fields.get("queue_drop"), 0) or 0
            session.ble_drop = _optional_int(fields.get("ble_drop"), 0) or 0
            session.ended_at = event.timestamp
            session.state = "DONE" if event_name == SUCCESS_EVENT else event_name.removeprefix(
                "CAPTURE_"
            )

        self._touch_session(session)

    def remember_firmware_status(
        self,
        node_mac: str,
        source_address: str,
        fields: dict[str, str],
    ) -> None:
        firmware = {
            key: value
            for key in ("fw", "firmware", "version", "build")
            if (value := fields.get(key))
        }
        if not firmware:
            return
        self._firmware_status[normalize_mac(node_mac)] = firmware
        self._firmware_status[normalize_mac(source_address)] = firmware

    def handle_disconnect(self, address: str, timestamp: float) -> None:
        normalized = normalize_mac(address)
        for session in self._sessions_for_address(normalized):
            if not session.terminal:
                session.connected = False
                session.updated_at = timestamp
                self._touch_session(session)

    def handle_command_result(
        self,
        address: str,
        command: str,
        succeeded: bool,
        error: str | None,
    ) -> None:
        if succeeded or not command.startswith(("sound_start", "sound_bg")):
            return
        normalized = normalize_mac(address)
        intent = self._pending_intent(normalized, normalized)
        if intent is not None:
            intent.transport_errors.append(error or "gatt_write_failed")
            intent.change_event.set()

    async def wait_for_request(
        self,
        request_id: int,
        *,
        terminal: bool,
        timeout: float,
    ) -> dict[str, object]:
        intent = self._intents[request_id]
        deadline = time.monotonic() + timeout
        while True:
            session = self._session_for_intent(intent)
            if intent.command_errors and session is None:
                self._remove_pending(intent)
                return {
                    "status": "failed",
                    "success": False,
                    "exit_code": 1,
                    "reason": intent.command_errors[-1],
                    "complete": False,
                    "session": {
                        "node_mac": intent.address,
                        "cid": None,
                        "label": intent.label,
                        "state": "REJECTED",
                        "location": "undefined",
                        "storage": "node_sd",
                    },
                }
            if session is not None:
                if terminal and session.terminal:
                    return self._outcome(session)
                if not terminal and session.accepted:
                    return self._outcome(session, accepted_only=True)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if session is None:
                    self._remove_pending(intent)
                return self._timeout_outcome(intent, session)
            intent.change_event.clear()
            session = self._session_for_intent(intent)
            if session is not None and (
                (terminal and session.terminal) or (not terminal and session.accepted)
            ):
                continue
            try:
                await asyncio.wait_for(intent.change_event.wait(), timeout=remaining)
            except TimeoutError:
                if session is None:
                    self._remove_pending(intent)
                return self._timeout_outcome(intent, session)

    async def wait_for_terminal_after(
        self,
        address: str,
        *,
        after: int,
        timeout: float,
    ) -> dict[str, object]:
        normalized = normalize_mac(address)
        status_event = self._status_events.setdefault(normalized, asyncio.Event())
        deadline = time.monotonic() + timeout
        while True:
            terminals = [
                session
                for session in self._sessions_for_address(normalized)
                if session.terminal and session.revision > after
            ]
            if terminals:
                return self._outcome(max(terminals, key=lambda item: item.revision))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "status": "failed",
                    "success": False,
                    "exit_code": 1,
                    "reason": "stop_timeout",
                    "complete": False,
                    "session": self.latest_session(normalized),
                }
            status_event.clear()
            try:
                await asyncio.wait_for(status_event.wait(), timeout=remaining)
            except TimeoutError:
                return {
                    "status": "failed",
                    "success": False,
                    "exit_code": 1,
                    "reason": "stop_timeout",
                    "complete": False,
                    "session": self.latest_session(normalized),
                }

    def status_revision(self, address: str) -> int:
        return self._status_revisions.get(normalize_mac(address), 0)

    async def wait_for_status_revision(
        self,
        address: str,
        *,
        after: int,
        timeout: float,
    ) -> bool:
        normalized = normalize_mac(address)
        if self._status_revisions.get(normalized, 0) > after:
            return True
        status_event = self._status_events.setdefault(normalized, asyncio.Event())
        deadline = time.monotonic() + timeout
        while self._status_revisions.get(normalized, 0) <= after:
            status_event.clear()
            if self._status_revisions.get(normalized, 0) > after:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(status_event.wait(), timeout=remaining)
            except TimeoutError:
                return False
        return True

    def latest_session(self, address: str) -> dict[str, object] | None:
        sessions = self._sessions_for_address(normalize_mac(address))
        if not sessions:
            return None
        return self._snapshot_session(max(sessions, key=lambda item: item.revision))

    def snapshot(self, address: str | None = None) -> object:
        normalized = normalize_mac(address) if address else None
        sessions = [
            self._snapshot_session(session)
            for session in self._sessions.values()
            if normalized is None
            or normalized in {session.node_mac, session.ble_address}
        ]
        sessions.sort(key=lambda item: (str(item["node_mac"]), str(item["cid"])))
        if normalized:
            return {
                "status": self._latest_status.get(normalized),
                "sessions": sessions,
                "storage": "node_sd",
            }
        statuses_by_node = {
            str(status["node_mac"]): status for status in self._latest_status.values()
        }
        return {
            "status": [statuses_by_node[key] for key in sorted(statuses_by_node)],
            "sessions": sessions,
            "storage": "node_sd",
        }

    def _remember_latest_status(
        self,
        node_mac: str,
        ble_address: str,
        event_name: str,
        event: ReportEvent,
    ) -> None:
        latest_status = {
            "node_mac": node_mac,
            "event": event_name,
            "state": event.packet.fields.get("state", ""),
            "label": event.packet.fields.get("label", ""),
            "cid": event.packet.fields.get("cid", ""),
            "timestamp": event.timestamp,
            "report": event.packet.message,
        }
        for address in {node_mac, ble_address}:
            self._latest_status[address] = latest_status
            self._status_revisions[address] = self._status_revisions.get(address, 0) + 1
            if status_event := self._status_events.get(address):
                status_event.set()

    def _touch_session(self, session: SoundCaptureSession) -> None:
        for address in {session.node_mac, session.ble_address}:
            session.revision = self._status_revisions.get(address, 0)
            if status_event := self._status_events.get(address):
                status_event.set()
        if session.request_id is not None:
            self._intents[session.request_id].change_event.set()

    def _pending_intent(
        self,
        node_mac: str,
        ble_address: str,
    ) -> SoundCommandIntent | None:
        candidates: list[SoundCommandIntent] = []
        for address in {node_mac, ble_address}:
            for request_id in self._pending_by_address.get(address, []):
                intent = self._intents[request_id]
                if intent.session_key is None:
                    candidates.append(intent)
        return max(candidates, key=lambda item: item.request_id) if candidates else None

    def _remove_pending(self, intent: SoundCommandIntent) -> None:
        pending = self._pending_by_address.get(intent.address, [])
        if intent.request_id in pending:
            pending.remove(intent.request_id)

    def _session_for_intent(
        self,
        intent: SoundCommandIntent,
    ) -> SoundCaptureSession | None:
        return self._sessions.get(intent.session_key) if intent.session_key else None

    def _sessions_for_address(self, address: str) -> list[SoundCaptureSession]:
        return [
            session
            for session in self._sessions.values()
            if address in {session.node_mac, session.ble_address}
        ]

    def _outcome(
        self,
        session: SoundCaptureSession,
        *,
        accepted_only: bool = False,
    ) -> dict[str, object]:
        if accepted_only and not session.terminal:
            return {
                "status": "armed",
                "success": True,
                "exit_code": 0,
                "reason": "accepted",
                "complete": None,
                "session": self._snapshot_session(session),
            }
        return {
            "status": "complete" if session.success else "failed",
            "success": session.success,
            "exit_code": 0 if session.success else 1,
            "reason": session.reason,
            "complete": session.complete,
            "session": self._snapshot_session(session),
        }

    def _timeout_outcome(
        self,
        intent: SoundCommandIntent,
        session: SoundCaptureSession | None,
    ) -> dict[str, object]:
        return {
            "status": "failed",
            "success": False,
            "exit_code": 1,
            "reason": "wait_timeout",
            "complete": False,
            "command_errors": list(intent.command_errors),
            "transport_errors": list(intent.transport_errors),
            "session": (
                self._snapshot_session(session)
                if session is not None
                else {
                    "node_mac": intent.address,
                    "cid": None,
                    "label": intent.label,
                    "state": "UNCONFIRMED",
                    "location": "undefined",
                    "storage": "node_sd",
                }
            ),
        }

    def _snapshot_session(self, session: SoundCaptureSession) -> dict[str, object]:
        return {
            "node_mac": session.node_mac,
            "ble_address": session.ble_address,
            "cid": session.cid,
            "label": session.label,
            "location": session.location,
            "state": session.state,
            "accepted": session.accepted,
            "terminal_event": session.terminal_event,
            "complete": session.complete,
            "reason": session.reason,
            "segment": session.segment,
            "samples": session.samples,
            "blocks": session.blocks,
            "segment_samples": session.segment_samples,
            "segment_blocks": session.segment_blocks,
            "queue_drop": session.queue_drop,
            "ble_drop": session.ble_drop,
            "connected": session.connected,
            "storage_errors": list(session.storage_errors),
            "storage": "node_sd",
        }


def _command_fields(command: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in command.split(",")[1:]:
        key, separator, value = part.partition("=")
        if separator:
            fields[key] = value
    return fields


def _capture_id(value: object) -> int | None:
    text = str(value or "").strip()
    if not CAPTURE_ID_PATTERN.fullmatch(text):
        return None
    return int(text, 16)


def _optional_int(value: object, default: int | None) -> int | None:
    try:
        return int(str(value), 10)
    except (TypeError, ValueError):
        return default


def _complete_flag(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _report_label(value: object) -> str:
    try:
        return validate_sound_label(str(value or "unknown"))
    except ValueError:
        return "unknown"
