from __future__ import annotations

import asyncio
import csv
from datetime import datetime
from pathlib import Path

from slimhub.config import DEFAULT_LOCATION, AppPaths
from slimhub.events import RawDataEvent
from slimhub.protocol.nus import normalize_mac


CSV_FIELDS = [
    "timestamp",
    "mac",
    "location",
    "flag_human_presence",
    "detected",
    "flag_env",
    "temperature_c",
    "humidity",
    "iaq",
    "eco2",
    "bvoc",
    "accuracy",
    "flag_sound",
    *[f"sound_{idx}" for idx in range(16)],
    "is_pir_human_detection_event",
]


class RawDataLogger:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._queue: asyncio.Queue[RawDataEvent | None] = asyncio.Queue()
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

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return
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

    def _path_for(self, event: RawDataEvent) -> Path:
        timestamp = datetime.fromtimestamp(event.timestamp)
        location = event.location or DEFAULT_LOCATION
        mac = normalize_mac(event.mac)
        return (
            self.paths.data_dir
            / location
            / mac
            / "rawdata"
            / f"{timestamp.strftime('%Y-%m-%d')}.csv"
        )

    def _row_for(self, event: RawDataEvent) -> dict[str, object]:
        timestamp = datetime.fromtimestamp(event.timestamp)
        packet = event.packet
        sound = list(packet.sound[:16])
        sound.extend([0] * (16 - len(sound)))

        row: dict[str, object] = {
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "mac": normalize_mac(event.mac),
            "location": event.location or DEFAULT_LOCATION,
            "flag_human_presence": packet.flag_human_presence,
            "detected": packet.detected,
            "flag_env": packet.flag_env,
            "temperature_c": f"{packet.temperature_c:.2f}",
            "humidity": packet.humidity,
            "iaq": packet.iaq,
            "eco2": packet.eco2,
            "bvoc": packet.bvoc,
            "accuracy": packet.accuracy,
            "flag_sound": packet.flag_sound,
            "is_pir_human_detection_event": int(packet.is_pir_human_detection_event),
        }
        for idx, value in enumerate(sound):
            row[f"sound_{idx}"] = value
        return row
