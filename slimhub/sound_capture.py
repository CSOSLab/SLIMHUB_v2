from __future__ import annotations

import json
import logging
import re
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from slimhub.config import AppPaths
from slimhub.events import ReportEvent
from slimhub.protocol.nus import AudioPacket, normalize_mac, validate_sound_label


SOUND_SAMPLE_RATE = 16000
SOUND_SAMPLE_WIDTH = 2
SOUND_CHANNELS = 1
ARMED_TIMEOUT_SECONDS = 120
ACTIVE_TIMEOUT_GRACE_SECONDS = 10
CAPTURE_ID_PATTERN = re.compile(r"^[0-9A-Fa-f]{1,8}$")
FINAL_EVENTS = {
    "CAPTURE_DONE",
    "CAPTURE_CANCELLED",
    "CAPTURE_STORAGE_ERROR",
    "CAPTURE_ERROR",
}


@dataclass(frozen=True)
class SoundCommandIntent:
    address: str
    payload: str
    label: str
    destination: str
    threshold_rms: int
    max_seconds: int
    silence_seconds: int
    created_at: float


@dataclass
class SoundCaptureSession:
    node_mac: str
    ble_address: str
    capture_id: int
    label: str
    intent: SoundCommandIntent
    state: str = "ARMED"
    armed_at: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    expected_block_sequence: int = 0
    expected_sample_offset: int = 0
    received_blocks: int = 0
    received_samples: int = 0
    received_bytes: int = 0
    reported_blocks: int | None = None
    reported_samples: int | None = None
    queue_drop: int = 0
    ble_drop: int = 0
    max_rms: int | None = None
    missing_ranges: list[dict[str, object]] = field(default_factory=list)
    reports: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    firmware: dict[str, str] = field(default_factory=dict)
    termination: str | None = None
    complete: bool = False
    wav_writer: Any = field(default=None, repr=False)

    @property
    def cid(self) -> str:
        return f"{self.capture_id:08x}"


class SoundCaptureStore:
    """Store explicitly requested BLE PCM captures as WAV plus JSON metadata."""

    def __init__(self, paths: AppPaths, logger: logging.Logger | None = None) -> None:
        self.paths = paths
        self.logger = logger or logging.getLogger(__name__)
        self._intents: dict[str, SoundCommandIntent] = {}
        self._sessions: dict[tuple[str, int], SoundCaptureSession] = {}
        self._latest_status: dict[str, dict[str, object]] = {}
        self._firmware_status: dict[str, dict[str, str]] = {}

    def register_command(self, address: str, command: str, timestamp: float) -> None:
        normalized = normalize_mac(address)
        if command.startswith("sound_start,"):
            fields = _command_fields(command)
            intent = SoundCommandIntent(
                address=normalized,
                payload=command,
                label=validate_sound_label(fields["label"]),
                destination=fields["dest"],
                threshold_rms=int(fields["thr"]),
                max_seconds=int(fields["max"]),
                silence_seconds=int(fields["silence"]),
                created_at=timestamp,
            )
            self._intents[normalized] = intent
        elif command.startswith("sound_bg,"):
            fields = _command_fields(command)
            self._intents[normalized] = SoundCommandIntent(
                address=normalized,
                payload=command,
                label="background",
                destination=fields["dest"],
                threshold_rms=0,
                max_seconds=int(fields["max"]),
                silence_seconds=0,
                created_at=timestamp,
            )

    def handle_report(self, event: ReportEvent) -> None:
        fields = event.packet.fields
        if fields.get("src", "").upper() != "SOUND":
            return
        node_mac = normalize_mac(event.mac)
        ble_address = normalize_mac(event.source_address)
        event_name = fields.get("event", "").upper()
        latest_status = {
            "node_mac": node_mac,
            "event": event_name,
            "state": fields.get("state", ""),
            "label": fields.get("label", ""),
            "cid": fields.get("cid", ""),
            "timestamp": event.timestamp,
            "report": event.packet.message,
        }
        self._latest_status[node_mac] = latest_status
        self._latest_status[ble_address] = latest_status

        capture_id = _capture_id(fields.get("cid"))
        if capture_id is None:
            if event_name not in {"CAPTURE_IDLE", "CAPTURE_BUSY", "COMMAND_ERROR"}:
                self.logger.warning(
                    "SOUND report missing/invalid cid mac=%s event=%s",
                    node_mac,
                    event_name,
                )
            return

        key = (node_mac, capture_id)
        session = self._sessions.get(key)
        if session is None and event_name in {
            "CAPTURE_ARMED",
            "CAPTURE_START",
            "CAPTURE_STATUS",
            "CAPTURE_STORAGE_WAIT",
            *FINAL_EVENTS,
        }:
            intent = self._intent_for(node_mac, ble_address)
            if intent is None:
                self.logger.warning(
                    "Ignoring unsolicited SOUND capture mac=%s cid=%08x event=%s",
                    node_mac,
                    capture_id,
                    event_name,
                )
                return
            session = SoundCaptureSession(
                node_mac=node_mac,
                ble_address=ble_address,
                capture_id=capture_id,
                label=intent.label,
                intent=intent,
                armed_at=event.timestamp,
                firmware=dict(
                    self._firmware_status.get(node_mac)
                    or self._firmware_status.get(ble_address)
                    or {}
                ),
            )
            self._sessions[key] = session
            self._intents.pop(node_mac, None)
            self._intents.pop(ble_address, None)

        if session is None:
            return

        self._remember_report(session, event_name, event)
        for key_name in ("fw", "firmware", "version", "build"):
            if value := fields.get(key_name):
                session.firmware[key_name] = value

        if event_name == "CAPTURE_ARMED":
            session.state = "ARMED"
            session.armed_at = session.armed_at or event.timestamp
            self._write_manifest(session)
            return

        if event_name == "CAPTURE_START":
            session.state = "ACTIVE"
            session.started_at = session.started_at or event.timestamp
            self._open_wav(session)
            self._write_manifest(session)
            return

        if event_name == "CAPTURE_STATUS":
            state = fields.get("state", "").upper()
            if state in {"IDLE", "ARMED", "ACTIVE"}:
                session.state = state
            self._write_manifest(session)
            return

        if event_name == "CAPTURE_STORAGE_WAIT":
            session.state = "ARMED"
            self._write_manifest(session)
            return

        if event_name in FINAL_EVENTS:
            self._finalize_from_report(session, event)

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
        normalized_node = normalize_mac(node_mac)
        normalized_source = normalize_mac(source_address)
        self._firmware_status[normalized_node] = firmware
        self._firmware_status[normalized_source] = firmware

    def handle_audio(
        self,
        node_mac: str,
        source_address: str,
        packet: AudioPacket,
        timestamp: float,
    ) -> bool:
        normalized_node = normalize_mac(node_mac)
        session = self._sessions.get((normalized_node, packet.capture_id))
        if session is None:
            self.logger.warning(
                "Discarding unsolicited AUDIO mac=%s cid=%08x block=%d",
                normalized_node,
                packet.capture_id,
                packet.block_sequence,
            )
            return False
        if session.intent.destination not in {"ble", "both"}:
            self.logger.warning(
                "Discarding unexpected AUDIO for sd-only capture mac=%s cid=%s",
                normalized_node,
                session.cid,
            )
            return False
        if session.state not in {"ARMED", "ACTIVE"}:
            return False
        if session.state == "ARMED":
            session.state = "ACTIVE"
            session.started_at = timestamp
            self._open_wav(session)

        if packet.block_sequence < session.expected_block_sequence or (
            packet.sample_offset < session.expected_sample_offset
        ):
            session.missing_ranges.append(
                {
                    "kind": "out_of_order_or_duplicate",
                    "block_sequence": packet.block_sequence,
                    "sample_offset": packet.sample_offset,
                }
            )
            return False

        if packet.block_sequence > session.expected_block_sequence:
            session.missing_ranges.append(
                {
                    "kind": "block_sequence",
                    "start": session.expected_block_sequence,
                    "end": packet.block_sequence - 1,
                }
            )
        if packet.sample_offset > session.expected_sample_offset:
            session.missing_ranges.append(
                {
                    "kind": "sample_offset",
                    "start": session.expected_sample_offset,
                    "end": packet.sample_offset - 1,
                }
            )

        if session.wav_writer is None:
            self._open_wav(session)
        session.wav_writer.writeframesraw(packet.pcm)
        session.received_blocks += 1
        session.received_samples += packet.sample_count
        session.received_bytes += packet.data_bytes
        session.expected_block_sequence = packet.block_sequence + 1
        session.expected_sample_offset = packet.sample_offset + packet.sample_count
        return True

    def handle_disconnect(self, address: str, timestamp: float) -> None:
        normalized = normalize_mac(address)
        for session in list(self._sessions.values()):
            if session.state not in {"ARMED", "ACTIVE"}:
                continue
            if normalized not in {session.node_mac, session.ble_address}:
                continue
            self._finalize(
                session,
                timestamp=timestamp,
                termination="disconnect",
                requested_complete=False,
            )

    def recover_timeouts(self, timestamp: float | None = None) -> int:
        now = time.time() if timestamp is None else timestamp
        recovered = 0
        for session in list(self._sessions.values()):
            if session.state == "ARMED" and session.armed_at is not None:
                expired = now >= session.armed_at + ARMED_TIMEOUT_SECONDS
            elif session.state == "ACTIVE" and session.started_at is not None:
                expired = now >= (
                    session.started_at
                    + session.intent.max_seconds
                    + ACTIVE_TIMEOUT_GRACE_SECONDS
                )
            else:
                expired = False
            if expired:
                self._finalize(
                    session,
                    timestamp=now,
                    termination="timeout",
                    requested_complete=False,
                )
                recovered += 1
        return recovered

    def close_all(self, timestamp: float | None = None) -> None:
        now = time.time() if timestamp is None else timestamp
        for session in list(self._sessions.values()):
            if session.state in {"ARMED", "ACTIVE"}:
                self._finalize(
                    session,
                    timestamp=now,
                    termination="daemon_stop",
                    requested_complete=False,
                )

    def snapshot(self, address: str | None = None, timestamp: float | None = None) -> object:
        now = time.time() if timestamp is None else timestamp
        normalized = normalize_mac(address) if address else None
        sessions = []
        for session in self._sessions.values():
            if normalized and normalized not in {session.node_mac, session.ble_address}:
                continue
            start = session.started_at or session.armed_at or now
            sessions.append(
                {
                    "node_mac": session.node_mac,
                    "ble_address": session.ble_address,
                    "cid": session.cid,
                    "label": session.label,
                    "state": session.state,
                    "elapsed_seconds": max(0.0, now - start),
                    "received_samples": session.received_samples,
                    "received_blocks": session.received_blocks,
                    "queue_drop": session.queue_drop,
                    "ble_drop": session.ble_drop,
                    "missing_ranges": list(session.missing_ranges),
                    "complete": session.complete,
                    "termination": session.termination,
                }
            )
        sessions.sort(key=lambda item: (str(item["node_mac"]), str(item["cid"])))
        if normalized:
            return {
                "status": self._latest_status.get(normalized),
                "sessions": sessions,
            }
        statuses_by_node = {
            str(status["node_mac"]): status
            for status in self._latest_status.values()
        }
        return {
            "status": [statuses_by_node[key] for key in sorted(statuses_by_node)],
            "sessions": sessions,
        }

    def _intent_for(
        self,
        node_mac: str,
        ble_address: str,
    ) -> SoundCommandIntent | None:
        return self._intents.get(node_mac) or self._intents.get(ble_address)

    def _open_wav(self, session: SoundCaptureSession) -> None:
        if session.wav_writer is not None:
            return
        if session.intent.destination not in {"ble", "both"}:
            return
        wav_path, _ = self._paths_for(session)
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        writer = wave.open(str(wav_path), "wb")
        writer.setnchannels(SOUND_CHANNELS)
        writer.setsampwidth(SOUND_SAMPLE_WIDTH)
        writer.setframerate(SOUND_SAMPLE_RATE)
        session.wav_writer = writer

    def _finalize_from_report(
        self,
        session: SoundCaptureSession,
        event: ReportEvent,
    ) -> None:
        fields = event.packet.fields
        session.queue_drop = _nonnegative_int(fields.get("queue_drop"), session.queue_drop)
        session.ble_drop = _nonnegative_int(fields.get("ble_drop"), session.ble_drop)
        session.max_rms = _optional_int(fields.get("max_rms"))
        session.reported_samples = _optional_int(fields.get("samples"))
        session.reported_blocks = _optional_int(fields.get("blocks"))
        if (
            session.reported_samples is not None
            and session.intent.destination in {"ble", "both"}
            and session.reported_samples != session.received_samples
        ):
            session.missing_ranges.append(
                {
                    "kind": "reported_sample_count",
                    "expected": session.reported_samples,
                    "received": session.received_samples,
                }
            )
        if (
            session.reported_blocks is not None
            and session.intent.destination in {"ble", "both"}
            and session.reported_blocks != session.received_blocks
        ):
            session.missing_ranges.append(
                {
                    "kind": "reported_block_count",
                    "expected": session.reported_blocks,
                    "received": session.received_blocks,
                }
            )
        event_name = fields.get("event", "").upper()
        requested_complete = event_name == "CAPTURE_DONE"
        termination = fields.get("reason") or event_name.lower()
        self._finalize(
            session,
            timestamp=event.timestamp,
            termination=termination,
            requested_complete=requested_complete,
        )

    def _finalize(
        self,
        session: SoundCaptureSession,
        *,
        timestamp: float,
        termination: str,
        requested_complete: bool,
    ) -> None:
        if session.wav_writer is not None:
            session.wav_writer.close()
            session.wav_writer = None
        session.ended_at = timestamp
        session.termination = termination
        if (
            requested_complete
            and session.intent.destination in {"ble", "both"}
            and session.received_blocks == 0
        ):
            session.missing_ranges.append({"kind": "no_audio_received"})
        session.complete = bool(
            requested_complete
            and not session.missing_ranges
            and session.queue_drop == 0
            and session.ble_drop == 0
        )
        session.state = "DONE" if requested_complete else "INCOMPLETE"
        self._write_manifest(session)
        if session.queue_drop or session.ble_drop or session.missing_ranges:
            self.logger.warning(
                "SOUND capture incomplete mac=%s cid=%s queue_drop=%d "
                "ble_drop=%d gaps=%d",
                session.node_mac,
                session.cid,
                session.queue_drop,
                session.ble_drop,
                len(session.missing_ranges),
            )

    def _remember_report(
        self,
        session: SoundCaptureSession,
        event_name: str,
        event: ReportEvent,
    ) -> None:
        session.reports.setdefault(event_name or "UNKNOWN", []).append(
            {
                "timestamp": _timestamp(event.timestamp),
                "raw": event.packet.message,
                "fields": dict(event.packet.fields),
            }
        )

    def _paths_for(self, session: SoundCaptureSession) -> tuple[Path, Path]:
        root = self.paths.data_dir / "sound" / session.node_mac / session.label
        return root / f"{session.cid}.wav", root / f"{session.cid}.json"

    def _write_manifest(self, session: SoundCaptureSession) -> None:
        wav_path, manifest_path = self._paths_for(session)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "node_mac": session.node_mac,
            "ble_address": session.ble_address,
            "cid": session.cid,
            "label": session.label,
            "command": {
                "payload": session.intent.payload,
                "destination": session.intent.destination,
                "threshold_rms": session.intent.threshold_rms,
                "max_seconds": session.intent.max_seconds,
                "silence_seconds": session.intent.silence_seconds,
                "requested_at": _timestamp(session.intent.created_at),
            },
            "timestamps": {
                "armed": _timestamp(session.armed_at),
                "start": _timestamp(session.started_at),
                "end": _timestamp(session.ended_at),
            },
            "audio": {
                "sample_rate": SOUND_SAMPLE_RATE,
                "bits": SOUND_SAMPLE_WIDTH * 8,
                "channels": SOUND_CHANNELS,
                "received_samples": session.received_samples,
                "received_blocks": session.received_blocks,
                "received_bytes": session.received_bytes,
                "reported_samples": session.reported_samples,
                "reported_blocks": session.reported_blocks,
                "missing_ranges": list(session.missing_ranges),
                "queue_drop": session.queue_drop,
                "ble_drop": session.ble_drop,
                "max_rms": session.max_rms,
                "wav": (
                    str(wav_path)
                    if session.intent.destination in {"ble", "both"}
                    else None
                ),
            },
            "state": session.state,
            "termination": session.termination,
            "complete": session.complete,
            "reports": session.reports,
            "firmware": session.firmware,
        }
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(manifest_path)


def _command_fields(command: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in command.split(",")[1:]:
        key, _, value = part.partition("=")
        fields[key] = value
    return fields


def _capture_id(value: object) -> int | None:
    text = str(value or "").strip()
    if not CAPTURE_ID_PATTERN.fullmatch(text):
        return None
    return int(text, 16)


def _optional_int(value: object) -> int | None:
    try:
        return int(str(value), 10)
    except (TypeError, ValueError):
        return None


def _nonnegative_int(value: object, default: int) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed >= 0 else default


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="milliseconds")
