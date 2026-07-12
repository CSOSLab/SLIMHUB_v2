from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime
from pathlib import Path

from slimhub.config import DEFAULT_DEVICE_TYPE, DEFAULT_LOCATION, AppPaths
from slimhub.events import AlertEvent, ConnectionStateEvent, RawDataEvent, ReportEvent
from slimhub.protocol.nus import normalize_mac


SOUND_CLASSLIST = [
    "background",
    "hitting",
    "speech_tv",
    "air_appliances",
    "brushing",
    "peeing",
    "flushing",
    "flush_end",
    "microwave",
    "cooking",
    "watering_low",
    "watering_high",
]

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


class RawDataLogger:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._queue: asyncio.Queue[
            RawDataEvent | AlertEvent | ReportEvent | ConnectionStateEvent | None
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

    async def write_alert(self, event: AlertEvent) -> None:
        path = self._alert_path_for(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(self._alert_line_for(event) + "\n")

    async def write_report(self, event: ReportEvent) -> None:
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
        sound = list(packet.sound[: len(SOUND_CLASSLIST)])
        sound.extend([0] * (len(SOUND_CLASSLIST) - len(sound)))

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
        for label, value in zip(SOUND_CLASSLIST, sound):
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
            "packet_type": "REPORT",
            "src": fields.get("src", ""),
            "event": fields.get("event", ""),
            "message": event.packet.message,
            "fields": fields,
        }
        for key in USD_STATUS_FIELDS:
            if key in fields:
                row[key] = fields[key]
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
        }
