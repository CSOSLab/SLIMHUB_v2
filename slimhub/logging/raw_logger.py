from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from slimhub.config import (
    DEFAULT_DEVICE_TYPE,
    DEFAULT_LOCATION,
    NODE_LOCATIONS,
    AppPaths,
    normalize_node_location,
)
from slimhub.events import (
    AlertEvent,
    ConnectionStateEvent,
    RawDataEvent,
    ReportEvent,
    StructuredEvent,
)
from slimhub.protocol.nus import normalize_mac


CANONICAL_SOUND_LABELS = (
    "background",
    "hitting",
    "speech_tv",
    "air_appliances",
    "brushing",
    "peeing",
    "flushing",
    "flushing_end",
    "watering_low",
    "watering_high",
    "cooking",
    "microwave",
    "appliances",
    "snoring",
    "gas_oven",
    "reserved",
)
BASE_CSV_FIELDS = [
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
]
CSV_FIELDS = [*BASE_CSV_FIELDS, *CANONICAL_SOUND_LABELS]
LEGACY_CSV_FIELDS = CSV_FIELDS
SEOUL = ZoneInfo("Asia/Seoul")
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
    "legacy_report_rejected",
    "multimodal_error",
    "raw_sound_metadata",
    "raw_location_quarantined",
    "raw_producer_rejected",
    "raw_write_error",
}
CONTRACT_RECORD_PREFIXES = ("node_", "config_", "inout_", "sound_", "location_")


class RawDataLogger:
    def __init__(
        self,
        paths: AppPaths,
        *,
        audit_mode: str | None = None,
        queue_size: int = 4096,
    ) -> None:
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
        ] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._path_locks: dict[Path, threading.Lock] = {}
        self._path_locks_guard = threading.Lock()
        self.health: dict[str, int] = {
            "written": 0,
            "queue_full": 0,
            "write_errors": 0,
            "quarantined": 0,
            "producer_rejected": 0,
        }

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

    def snapshot(self) -> dict[str, int]:
        return {**self.health, "queued": self._queue.qsize()}

    async def log(self, event: RawDataEvent) -> None:
        if self._task is None:
            await self.write_event(event)
            return
        self._put_nowait(event)

    async def log_alert(self, event: AlertEvent) -> None:
        if self._task is None:
            await self.write_alert(event)
            return
        self._put_nowait(event)

    async def log_report(self, event: ReportEvent) -> None:
        if self._task is None:
            await self.write_report(event)
            return
        self._put_nowait(event)

    async def log_connection_state(self, event: ConnectionStateEvent) -> None:
        if self._task is None:
            await self.write_connection_state(event)
            return
        self._put_nowait(event)

    async def log_structured(self, event: StructuredEvent) -> None:
        if self._task is None:
            await self.write_structured(event)
            return
        self._put_nowait(event)

    def _put_nowait(
        self,
        event: RawDataEvent
        | AlertEvent
        | ReportEvent
        | ConnectionStateEvent
        | StructuredEvent,
    ) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.health["queue_full"] += 1
            logging.error(
                "Raw writer queue full event=%s mac=%s",
                type(event).__name__,
                getattr(event, "mac", getattr(event, "address", "unknown")),
            )

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
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
            finally:
                self._queue.task_done()

    async def write_event(self, event: RawDataEvent) -> None:
        location = normalize_node_location(event.location)
        if location not in NODE_LOCATIONS:
            self.health["quarantined"] += 1
            await self.write_structured(
                StructuredEvent(
                    timestamp=event.receipt_timestamp or event.timestamp,
                    kind="raw_location_quarantined",
                    mac=event.mac,
                    data={
                        "location": event.location or DEFAULT_LOCATION,
                        "reason": "unknown_legacy_location",
                    },
                )
            )
            return
        producer = self._producer_for(event)
        if producer is None:
            self.health["producer_rejected"] += 1
            await self.write_structured(
                StructuredEvent(
                    timestamp=event.receipt_timestamp or event.timestamp,
                    kind="raw_producer_rejected",
                    mac=event.mac,
                    data={
                        "location": location,
                        "reason": "exactly_one_of_GridEye_ENV_SOUND_must_equal_1",
                        "flags": {
                            "GridEye": event.packet.flag_human_presence,
                            "ENV": event.packet.flag_env,
                            "SOUND": event.packet.flag_sound,
                        },
                    },
                )
            )
            return
        try:
            if self._task is None:
                self._write_event_sync(event, location, producer)
            else:
                await asyncio.to_thread(
                    self._write_event_sync,
                    event,
                    location,
                    producer,
                )
        except (OSError, ValueError) as exc:
            self.health["write_errors"] += 1
            logging.exception(
                "RAWDATA write failed mac=%s location=%s", event.mac, location
            )
            await self.write_structured(
                StructuredEvent(
                    timestamp=event.receipt_timestamp or event.timestamp,
                    kind="raw_write_error",
                    mac=event.mac,
                    data={"location": location, "error": str(exc)},
                )
            )
            return
        self.health["written"] += 1
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
                        "sound_raw_schema": event.sound_raw_schema,
                    },
                )
            )

    async def write_alert(self, event: AlertEvent) -> None:
        path = self._alert_path_for(event)
        await self._append_text(path, self._alert_line_for(event) + "\n")

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
        await self._append_text(
            path,
            json.dumps(
                self._structured_report_row_for(event),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )

    async def write_connection_state(self, event: ConnectionStateEvent) -> None:
        if self.audit_mode != "full":
            return
        path = self._structured_path_for(event.timestamp)
        await self._append_text(
            path,
            json.dumps(
                self._connection_state_row_for(event),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
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
        timestamp = datetime.fromtimestamp(event.timestamp, SEOUL)
        row: dict[str, object] = {
            "kind": event.kind,
            "time": timestamp.isoformat(timespec="milliseconds"),
            "receipt_ts": event.timestamp,
            "mac": normalize_mac(event.mac),
            **event.data,
        }
        await self._append_text(
            path,
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n",
        )

    def _path_for(self, event: RawDataEvent, *, location: str) -> Path:
        timestamp = datetime.fromtimestamp(event.timestamp, SEOUL)
        mac = normalize_mac(event.mac)
        device_type = DEFAULT_DEVICE_TYPE
        filename = f"{timestamp.strftime('%Y-%m-%d')}.txt"
        return (
            self.paths.data_dir
            / location
            / device_type
            / mac
            / "inference"
            / "rawdata"
            / filename
        )

    def _alert_path_for(self, event: AlertEvent) -> Path:
        timestamp = datetime.fromtimestamp(event.timestamp, SEOUL)
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
        date = datetime.fromtimestamp(timestamp, SEOUL).strftime("%Y-%m-%d")
        return self.paths.programdata_dir / "reports" / f"{date}.jsonl"

    def _write_event_sync(
        self,
        event: RawDataEvent,
        location: str,
        producer: str,
    ) -> None:
        path = self._path_for(event, location=location)
        lock = self._lock_for(path)
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._migrate_incompatible_header(path, CSV_FIELDS)
            needs_header = not path.exists() or path.stat().st_size == 0
            row = self._row_for(event, location=location, producer=producer)
            if set(row) != set(CSV_FIELDS):
                raise ValueError("RAWDATA CSV row does not match union header")
            if (
                int(row["GridEye"]) + int(row["ENV"]) + int(row["SOUND"])
                != 1
            ):
                raise ValueError("RAWDATA producer exclusivity invariant failed")
            with path.open("a", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=CSV_FIELDS,
                    extrasaction="raise",
                    lineterminator="\n",
                )
                if needs_header:
                    writer.writeheader()
                writer.writerow(row)
                stream.flush()

    @staticmethod
    def _producer_for(event: RawDataEvent) -> str | None:
        packet = event.packet
        if packet.flag_human_presence == 1 and packet.detected not in {10, 20}:
            return None
        enabled = [
            name
            for name, flag in (
                ("GridEye", packet.flag_human_presence),
                ("ENV", packet.flag_env),
                ("SOUND", packet.flag_sound),
            )
            if flag == 1
        ]
        return enabled[0] if len(enabled) == 1 else None

    def _row_for(
        self,
        event: RawDataEvent,
        *,
        location: str,
        producer: str,
    ) -> dict[str, object]:
        timestamp = datetime.fromtimestamp(event.timestamp, SEOUL)
        packet = event.packet
        row: dict[str, object] = {
            "time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "GridEye": 0,
            "Direction": 0,
            "ENV": 0,
            "temp": 0,
            "humid": 0,
            "iaq": 0,
            "eco2": 0,
            "bvoc": 0,
            "SOUND": 0,
            **{label: 0.0 for label in CANONICAL_SOUND_LABELS},
        }
        if producer == "GridEye":
            row["GridEye"] = 1
            row["Direction"] = packet.detected
        elif producer == "ENV":
            row.update(
                {
                    "ENV": 1,
                    "temp": f"{packet.temperature_c:.2f}",
                    "humid": packet.humidity,
                    "iaq": packet.iaq,
                    "eco2": packet.eco2,
                    "bvoc": packet.bvoc,
                }
            )
        elif producer == "SOUND":
            row["SOUND"] = 1
            for label, score in self._sound_scores(event):
                row[label] = (score + 128) / 256
        else:
            raise ValueError(f"unknown RAWDATA producer: {producer}")
        return row

    def _sound_scores(
        self,
        event: RawDataEvent,
    ) -> list[tuple[str, int]]:
        return list(zip(CANONICAL_SOUND_LABELS, event.packet.sound))

    async def _append_text(self, path: Path, text: str) -> None:
        try:
            if self._task is None:
                self._append_text_sync(path, text)
            else:
                await asyncio.to_thread(self._append_text_sync, path, text)
        except OSError:
            self.health["write_errors"] += 1
            logging.exception("Writer append failed path=%s", path)

    def _append_text_sync(self, path: Path, text: str) -> None:
        lock = self._lock_for(path)
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="") as stream:
                stream.write(text)
                stream.flush()

    def _lock_for(self, path: Path) -> threading.Lock:
        with self._path_locks_guard:
            return self._path_locks.setdefault(path, threading.Lock())

    @staticmethod
    def _migrate_incompatible_header(path: Path, fieldnames: list[str]) -> None:
        if not path.exists() or path.stat().st_size == 0:
            return
        with path.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        if not rows:
            return
        if rows[0] == fieldnames:
            return

        source_fields = [
            "flushing_end" if name == "flush_end" else name
            for name in rows[0]
        ]
        if len(source_fields) != len(set(source_fields)):
            raise ValueError("legacy RAWDATA header contains duplicate columns")
        if "time" not in source_fields:
            raise ValueError("legacy RAWDATA header is missing time")

        sound_fields = set(CANONICAL_SOUND_LABELS)
        migrated_rows: list[list[object]] = []
        for values in rows[1:]:
            source = {
                name: values[index]
                for index, name in enumerate(source_fields)
                if index < len(values)
            }
            migrated_rows.append(
                [
                    source.get(
                        name,
                        0.0 if name in sound_fields else 0,
                    )
                    for name in fieldnames
                ]
            )

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_name = stream.name
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(fieldnames)
                writer.writerows(migrated_rows)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
        except Exception:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink()
                except FileNotFoundError:
                    pass
            raise

    def _alert_line_for(self, event: AlertEvent) -> str:
        timestamp = datetime.fromtimestamp(event.timestamp, SEOUL)
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
        timestamp = datetime.fromtimestamp(event.timestamp, SEOUL)
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
        timestamp = datetime.fromtimestamp(event.timestamp, SEOUL)
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
            or fields.get("event", "").upper()
            in {
                "ENTER",
                "EXIT",
                "CONFIRM_ACK",
                "CONFIRM_ERROR",
            }
        )
    return src in {"EVENT", "ADL"} and fields.get("schema") == "2"
