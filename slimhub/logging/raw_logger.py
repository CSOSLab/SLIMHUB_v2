from __future__ import annotations

import asyncio
import csv
import json
import os
from datetime import datetime
from pathlib import Path

from slimhub.config import DEFAULT_DEVICE_TYPE, DEFAULT_LOCATION, AppPaths
from slimhub.events import (
    AlertEvent,
    ConnectionStateEvent,
    RawDataEvent,
    ReportEvent,
    StructuredEvent,
)
from slimhub.logging.sound_schema import B_TFLM_V1_SCHEMA, resolve_sound_schema
from slimhub.protocol.nus import normalize_mac


SOUND_CLASSLIST = list(B_TFLM_V1_SCHEMA.labels)

CSV_FIELDS = [
    "time",
    "GridEye",
    "Direction",
    "ENV",
    "temp",
    "humid",
    "iaq",
    "eco2",
    "bvoc",
    "SOUND",
    *SOUND_CLASSLIST,
]

USD_STATUS_FIELDS = (
    "batt_mv",
    "batt_v",
    "batt_pct",
    "batt_rem_mah",
    "usb",
    "chg",
    "sd",
    "file",
    "uptime",
    "ok",
)

AUDIT_MODES = {"off", "minimal", "full"}
MINIMAL_AUDIT_KINDS = {
    "command_failed",
    "legacy_event_invalid",
    "legacy_inference_invalid",
    "legacy_json_invalid",
    "multimodal_error",
    "raw_sound_metadata",
}
CONTRACT_RECORD_PREFIXES = ("node_", "config_", "inout_", "sound_")


class RawDataLogger:
    def __init__(self, paths: AppPaths, *, audit_mode: str | None = None) -> None:
        self.paths = paths
        selected_audit_mode = (
            audit_mode
            if audit_mode is not None
            else os.environ.get("SLIMHUB_AUDIT_JSONL", "minimal")
        )
        self.audit_mode = selected_audit_mode.strip().lower()
        if self.audit_mode not in AUDIT_MODES:
            raise ValueError(
                "SLIMHUB_AUDIT_JSONL must be one of: off, minimal, full"
            )
        self._queue: asyncio.Queue[
            RawDataEvent
            | AlertEvent
            | ReportEvent
            | ConnectionStateEvent
            | StructuredEvent
            | None
        ] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self.paths.ensure()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def log(self, event: RawDataEvent) -> None:
        if self._task is None:
            await self.write_event(event)
            return
        await self._queue.put(event)

    async def log_alert(self, event: AlertEvent) -> None:
        if self._task is None:
            await self.write_alert(event)
            return
        await self._queue.put(event)

    async def log_report(self, event: ReportEvent) -> None:
        if self._task is None:
            await self.write_report(event)
            return
        await self._queue.put(event)

    async def log_connection_state(self, event: ConnectionStateEvent) -> None:
        if self._task is None:
            await self.write_connection_state(event)
            return
        await self._queue.put(event)

    async def log_structured(self, event: StructuredEvent) -> None:
        if self._task is None:
            await self.write_structured(event)
            return
        await self._queue.put(event)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            if isinstance(event, AlertEvent):
                await self.write_alert(event)
            elif isinstance(event, ReportEvent):
                await self.write_report(event)
            elif isinstance(event, ConnectionStateEvent):
                await self.write_connection_state(event)
            elif isinstance(event, StructuredEvent):
                await self.write_structured(event)
            else:
                await self.write_event(event)

    async def write_event(self, event: RawDataEvent) -> None:
        path = self._path_for(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(self._row_for(event))
        if self.audit_mode == "full":
            await self.write_structured(
                StructuredEvent(
                    timestamp=event.receipt_timestamp or event.timestamp,
                    kind="raw",
                    mac=event.mac,
                    data={
                        "ble_address": event.source_address,
                        "location": event.location or DEFAULT_LOCATION,
                        "device_type": event.device_type or DEFAULT_DEVICE_TYPE,
                        "session_id": event.session_id,
                        "packet_type": "RAWDATA",
                        "raw_payload_hex": event.payload.hex(),
                        "parsed": {
                            "flag_human_presence": event.packet.flag_human_presence,
                            "detected": event.packet.detected,
                            "flag_env": event.packet.flag_env,
                            "flag_sound": event.packet.flag_sound,
                        },
                            "sound_schema_version": event.sound_schema_version,
                            "sound_class_count": event.sound_class_count,
                            "sound_semantic_ready": event.sound_semantic_ready,
                            "sound_profile": event.sound_profile,
                            "sound_model": event.sound_model,
                    },
                )
            )

    async def write_alert(self, event: AlertEvent) -> None:
        path = self._alert_path_for(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(self._alert_line_for(event) + "\n")

    async def write_report(self, event: ReportEvent) -> None:
        if self.audit_mode == "off":
            return
        if self.audit_mode == "minimal" and not (
            event.packet.parse_error
            or event.identity_warning
            or _retain_contract_report(event)
        ):
            return
        path = self._structured_path_for(event.timestamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    self._structured_report_row_for(event),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    async def write_connection_state(self, event: ConnectionStateEvent) -> None:
        if self.audit_mode != "full":
            return
        path = self._structured_path_for(event.timestamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    self._connection_state_row_for(event),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    async def write_structured(self, event: StructuredEvent) -> None:
        if self.audit_mode == "off":
            return
        if (
            self.audit_mode == "minimal"
            and event.kind not in MINIMAL_AUDIT_KINDS
            and not event.kind.startswith(CONTRACT_RECORD_PREFIXES)
        ):
            return
        path = self._structured_path_for(event.timestamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.fromtimestamp(event.timestamp)
        row: dict[str, object] = {
            "kind": event.kind,
            "time": timestamp.isoformat(timespec="milliseconds"),
            "receipt_ts": event.timestamp,
            "mac": normalize_mac(event.mac),
            **event.data,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def _path_for(self, event: RawDataEvent) -> Path:
        timestamp = datetime.fromtimestamp(event.timestamp)
        location = event.location or DEFAULT_LOCATION
        mac = normalize_mac(event.mac)
        device_type = event.device_type or DEFAULT_DEVICE_TYPE
        return (
            self.paths.data_dir
            / location
            / device_type
            / mac
            / "inference"
            / "rawdata"
            / f"{timestamp.strftime('%Y-%m-%d')}.txt"
        )

    def _alert_path_for(self, event: AlertEvent) -> Path:
        timestamp = datetime.fromtimestamp(event.timestamp)
        location = event.location or DEFAULT_LOCATION
        mac = normalize_mac(event.mac)
        device_type = event.device_type or DEFAULT_DEVICE_TYPE
        return (
            self.paths.data_dir
            / location
            / device_type
            / mac
            / "inference"
            / "debugstr"
            / f"{timestamp.strftime('%Y-%m-%d')}.txt"
        )

    def _structured_path_for(self, timestamp: float) -> Path:
        date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        return self.paths.programdata_dir / "reports" / f"{date}.jsonl"

    def _row_for(self, event: RawDataEvent) -> dict[str, object]:
        timestamp = datetime.fromtimestamp(event.timestamp)
        packet = event.packet
        sound: list[int | None]
        if event.sound_semantic_ready is False:
            schema = B_TFLM_V1_SCHEMA
            sound = [None] * schema.class_count
        else:
            try:
                selector = event.sound_profile or event.sound_schema_version
                if not selector:
                    raise ValueError(
                        "legacy sound scores require a per-device schema"
                    )
                schema = resolve_sound_schema(
                    selector,
                    event.sound_class_count,
                )
                sound = list(packet.sound[: schema.class_count])
                sound.extend([None] * (schema.class_count - len(sound)))
                if schema.labels != B_TFLM_V1_SCHEMA.labels:
                    # The legacy CSV has fixed toilet-v1 columns. Dynamic room
                    # semantics are retained in raw_sound_metadata JSONL instead
                    # of writing scores under incorrect legacy headings.
                    schema = B_TFLM_V1_SCHEMA
                    sound = [None] * schema.class_count
            except ValueError:
                # Preserve the raw packet and avoid inventing labels if a firmware
                # report advertises an incompatible schema.
                schema = B_TFLM_V1_SCHEMA
                sound = [None] * schema.class_count

        row: dict[str, object] = {
            "time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "GridEye": packet.flag_human_presence,
            "Direction": packet.detected,
            "ENV": packet.flag_env,
            "temp": f"{packet.temperature_c:.2f}",
            "humid": packet.humidity,
            "iaq": packet.iaq,
            "eco2": packet.eco2,
            "bvoc": packet.bvoc,
            "SOUND": packet.flag_sound,
        }
        for label, value in zip(schema.labels, sound):
            # A zero byte is the firmware's padding for an untransmitted score;
            # it must not become a plausible-looking 0.5 probability.
            if not packet.flag_sound or value is None or value == 0:
                row[label] = ""
            else:
                row[label] = (value + 128) / 256
        return row
    def _alert_line_for(self, event: AlertEvent) -> str:
        timestamp = datetime.fromtimestamp(event.timestamp)
        timestamp_text = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        try:
            payload = json.loads(event.packet.message)
        except json.JSONDecodeError:
            line = event.packet.message.rstrip("\n")
            return f"{timestamp_text},{line}"

        if isinstance(payload, dict):
            payload["timestamp"] = timestamp_text
            return json.dumps(payload, ensure_ascii=False)
        return f"{timestamp_text},{event.packet.message.rstrip()}"

    def _structured_report_row_for(self, event: ReportEvent) -> dict[str, object]:
        timestamp = datetime.fromtimestamp(event.timestamp)
        fields = dict(event.packet.fields)
        row: dict[str, object] = {
            "kind": "report",
            "time": timestamp.isoformat(timespec="milliseconds"),
            "timestamp": event.timestamp,
            "mac": normalize_mac(event.mac),
            "ble_address": normalize_mac(event.source_address),
            "location": event.location or DEFAULT_LOCATION,
            "device_type": event.device_type or DEFAULT_DEVICE_TYPE,
            "connected": event.connected,
            "session_id": event.session_id,
            "receipt_ts": event.receipt_timestamp or event.timestamp,
            "clock_offset_ms": event.clock_offset_ms,
            "clock_error_ms": event.clock_error_ms,
            "wrap_epoch": event.wrap_epoch,
            "packet_type": "REPORT",
            "raw_payload_hex": event.payload.hex(),
            "src": fields.get("src", "")
            or ("LEGACY_JSON" if event.packet.format == "json" else ""),
            "event": fields.get("event", "") or fields.get("status", ""),
            "message": event.packet.message,
            "fields": fields,
            "report_format": event.packet.format,
            "json_document": event.packet.document,
            "parse_error": event.packet.parse_error,
            "duplicate_fields": event.packet.duplicate_fields,
            "identity_warning": event.identity_warning,
        }
        for key in USD_STATUS_FIELDS:
            if key in fields:
                row[key] = fields[key]
        for key in ("boot_id", "primary_seq", "event_seq", "event_id", "id", "event_ts_ms", "cmd_id", "target_match", "occupied", "source_count"):
            if key in fields:
                row[key] = fields[key]
        event_id = str(fields.get("event_id") or fields.get("id") or "").upper()
        result = str(fields.get("result") or "").upper()
        if event_id in {"C0", "C1"}:
            row["classification"] = "ack"
        elif event_id in {"D0", "D1"} or result.endswith("CONFIRMED"):
            row["classification"] = "transition"
        else:
            row["classification"] = "report"
        return row

    def _connection_state_row_for(
        self,
        event: ConnectionStateEvent,
    ) -> dict[str, object]:
        timestamp = datetime.fromtimestamp(event.timestamp)
        address = normalize_mac(event.address)
        return {
            "kind": "connection",
            "time": timestamp.isoformat(timespec="milliseconds"),
            "timestamp": event.timestamp,
            "mac": address,
            "ble_address": address,
            "connected": event.connected,
            "session_id": event.session_id,
        }


def _retain_contract_report(event: ReportEvent) -> bool:
    fields = event.packet.fields
    src = fields.get("src", "").strip().upper()
    if src in {"NODE", "CONFIG", "SOUND"}:
        return True
    if src == "INOUT":
        return (
            fields.get("schema") == "2"
            or bool(fields.get("bid") or fields.get("boot_id"))
            or fields.get("event", "").upper() in {"CONFIRM_ACK", "CONFIRM_ERROR"}
        )
    return src in {"EVENT", "ADL"} and fields.get("schema") == "2"
