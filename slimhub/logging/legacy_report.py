from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from slimhub.config import (
    DEFAULT_DEVICE_TYPE,
    NODE_LOCATIONS,
    AppPaths,
    normalize_node_location,
)
from slimhub.protocol.nus import normalize_mac


SEOUL = ZoneInfo("Asia/Seoul")
MIN_SYNCED_EPOCH = 1_577_836_800.0  # 2020-01-01T00:00:00Z
LEGACY_DEDUPE_TTL_SECONDS = 4.0
LEGACY_INFERENCE_STATUSES = frozenset(
    {"PRE-DETECT", "COMPLETE", "PARTIAL", "NO_MATCH", "POP"}
)
DEBUG_KEYS = ("device", "type", "event", "value")
INFERENCE_KEYS = (
    "device",
    "type",
    "ADL",
    "status",
    "sequence",
    "sequence_list",
    "truth",
    "missing",
)


class LegacyReportValidationError(ValueError):
    """A strict legacy REPORT cannot be allowed to mutate legacy files."""


@dataclass(frozen=True)
class ValidatedLegacyReport:
    mac: str
    kind: str
    document: dict[str, object]
    fingerprint: tuple[object, ...]


@dataclass(frozen=True)
class LegacyWriteItem:
    report: ValidatedLegacyReport
    timestamp: float
    location: str
    device_type: str


def validate_legacy_report(
    transport_mac: str,
    document: object,
) -> ValidatedLegacyReport:
    """Validate and project a DEAN v2 strict legacy JSON object."""
    mac = normalize_mac(transport_mac)
    if not isinstance(document, dict):
        raise LegacyReportValidationError("json_report_must_be_an_object")

    device = document.get("device")
    if not isinstance(device, str) or not device.strip():
        raise LegacyReportValidationError("missing_device")
    try:
        reported_mac = normalize_mac(device)
    except ValueError as exc:
        raise LegacyReportValidationError("invalid_device") from exc
    if reported_mac != mac:
        raise LegacyReportValidationError("device_mismatch")

    record_type = document.get("type")
    if not isinstance(record_type, str):
        raise LegacyReportValidationError("missing_type")
    kind = record_type

    if kind == "DEBUG":
        _require_keys(document, DEBUG_KEYS)
        action = document.get("event")
        value = document.get("value")
        if not isinstance(action, str):
            raise LegacyReportValidationError("invalid_debug_event")
        if action not in {"ENTER", "EXIT"}:
            raise LegacyReportValidationError("invalid_debug_event")
        expected = 10 if action == "ENTER" else 20
        if isinstance(value, bool) or not isinstance(value, int) or value != expected:
            raise LegacyReportValidationError("invalid_debug_value")
        projected: dict[str, object] = {
            "device": mac,
            "type": "DEBUG",
            "event": action,
            "value": value,
        }
        fingerprint = (mac, "DEBUG", action, value)
    elif kind == "INFERENCE":
        _require_keys(document, INFERENCE_KEYS)
        status = document.get("status")
        if not isinstance(status, str):
            raise LegacyReportValidationError("invalid_inference_status")
        if status not in LEGACY_INFERENCE_STATUSES:
            raise LegacyReportValidationError("invalid_inference_status")
        for key in ("ADL", "sequence", "sequence_list", "missing"):
            if not isinstance(document.get(key), str):
                raise LegacyReportValidationError(
                    f"invalid_inference_{key.lower()}"
                )
        truth = document.get("truth")
        if (
            isinstance(truth, bool)
            or not isinstance(truth, (int, float))
            or not math.isfinite(float(truth))
        ):
            raise LegacyReportValidationError("invalid_inference_truth")
        projected = {
            "device": mac,
            "type": "INFERENCE",
            "ADL": document["ADL"],
            "status": status,
            "sequence": document["sequence"],
            "sequence_list": document["sequence_list"],
            "truth": truth,
            "missing": document["missing"],
        }
        fingerprint = (
            mac,
            "INFERENCE",
            document["ADL"],
            status,
            document["sequence"],
            truth,
            document["missing"],
        )
    else:
        raise LegacyReportValidationError("unsupported_legacy_type")

    return ValidatedLegacyReport(
        mac=mac,
        kind=kind,
        document=projected,
        fingerprint=fingerprint,
    )


def _require_keys(document: dict[object, object], required: tuple[str, ...]) -> None:
    missing = [key for key in required if key not in document]
    if missing:
        raise LegacyReportValidationError(f"missing_field:{missing[0]}")


class LegacyReportWriter:
    """Bounded, MAC-safe writer for the NCS-compatible legacy timeline."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        queue_size: int = 2048,
        pending_size: int = 512,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.paths = paths
        self._queue: asyncio.Queue[LegacyWriteItem | None] = asyncio.Queue(
            maxsize=queue_size
        )
        self._pending: deque[
            tuple[ValidatedLegacyReport, str, str]
        ] = deque(maxlen=pending_size)
        self._task: asyncio.Task[None] | None = None
        self._monotonic = monotonic
        self._seen: OrderedDict[tuple[object, ...], float] = OrderedDict()
        self._state_lock = asyncio.Lock()
        self._path_locks: dict[Path, threading.Lock] = {}
        self._path_locks_guard = threading.Lock()
        self.health: dict[str, int] = {
            "accepted": 0,
            "written": 0,
            "deduplicated": 0,
            "queue_full": 0,
            "pending": 0,
            "pending_overflow": 0,
            "write_errors": 0,
        }

    async def start(self) -> None:
        if self._task is None:
            self.paths.ensure()
            self._task = asyncio.create_task(
                self._run(),
                name="legacy-report-writer",
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def log(
        self,
        report: ValidatedLegacyReport,
        *,
        timestamp: float,
        location: str,
        device_type: str = DEFAULT_DEVICE_TYPE,
        wait_for_commit: bool = False,
    ) -> str:
        normalized_location = normalize_node_location(location)
        if normalized_location not in NODE_LOCATIONS:
            return "unknown_location"

        items: list[LegacyWriteItem] = []
        async with self._state_lock:
            now = self._monotonic()
            self._expire_seen(now)
            if report.fingerprint in self._seen:
                self.health["deduplicated"] += 1
                return "deduplicated"
            self._seen[report.fingerprint] = now + LEGACY_DEDUPE_TTL_SECONDS
            self.health["accepted"] += 1

            if timestamp < MIN_SYNCED_EPOCH:
                if len(self._pending) == self._pending.maxlen:
                    self._pending.popleft()
                    self.health["pending_overflow"] += 1
                    logging.error(
                        "Legacy pending time-sync queue overflow mac=%s",
                        report.mac,
                    )
                self._pending.append(
                    (report, normalized_location, device_type or DEFAULT_DEVICE_TYPE)
                )
                self.health["pending"] = len(self._pending)
                return "pending_time_sync"

            while self._pending:
                pending_report, pending_location, pending_device_type = (
                    self._pending.popleft()
                )
                items.append(
                    LegacyWriteItem(
                        report=pending_report,
                        timestamp=timestamp,
                        location=pending_location,
                        device_type=DEFAULT_DEVICE_TYPE,
                    )
                )
            self.health["pending"] = 0
            items.append(
                LegacyWriteItem(
                    report=report,
                    timestamp=timestamp,
                    location=normalized_location,
                    device_type=DEFAULT_DEVICE_TYPE,
                )
            )

        write_errors_before = self.health["write_errors"]
        if wait_for_commit and self._task is not None:
            # Preserve FIFO order with previously queued reports, then perform
            # this small DEBUG write inline so returning "written" is the
            # handoff's durable file-commit barrier.
            while self._queue._unfinished_tasks:  # noqa: SLF001
                await asyncio.sleep(0.001)
            for item in items:
                try:
                    self._write_item(item)
                except OSError:
                    self.health["write_errors"] += 1
                    logging.exception(
                        "Legacy report write failed mac=%s location=%s",
                        item.report.mac,
                        item.location,
                    )
                    return "write_error"
            return "written"
        for item in items:
            if self._task is None:
                try:
                    self._write_item(item)
                except OSError:
                    self.health["write_errors"] += 1
                    logging.exception(
                        "Legacy report write failed mac=%s location=%s",
                        item.report.mac,
                        item.location,
                    )
                    return "write_error"
                continue
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self.health["queue_full"] += 1
                logging.error(
                    "Legacy report writer queue full mac=%s location=%s",
                    item.report.mac,
                    item.location,
                )
                return "queue_full"
        if self.health["write_errors"] > write_errors_before:
            return "write_error"
        return "written" if self._task is None else "queued"

    def snapshot(self) -> dict[str, int]:
        return {**self.health, "queued": self._queue.qsize()}

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                await asyncio.to_thread(self._write_item, item)
            except OSError:
                self.health["write_errors"] += 1
                if item is not None:
                    logging.exception(
                        "Legacy report write failed mac=%s location=%s",
                        item.report.mac,
                        item.location,
                    )
            finally:
                self._queue.task_done()

    def _write_item(self, item: LegacyWriteItem) -> None:
        time_value = datetime.fromtimestamp(item.timestamp, SEOUL)
        timestamp_text = time_value.strftime("%Y-%m-%d %H:%M:%S")
        date = time_value.strftime("%Y-%m-%d")
        document = dict(item.report.document)
        document["timestamp"] = timestamp_text
        debug_line = json.dumps(document, ensure_ascii=False) + "\n"
        display_line = (
            f"{timestamp_text}  {item.location} "
            f"{_display_message(document)}\n"
        )

        debug_path = (
            self.paths.data_dir
            / item.location
            / item.device_type
            / item.report.mac
            / "inference"
            / "debugstr"
            / f"{date}.txt"
        )
        daily_display_path = self.paths.display_dir / f"{date}.txt"
        current_display_path = self.paths.display_path

        self._append_locked(
            debug_path,
            debug_line,
        )
        self._append_locked(
            daily_display_path,
            display_line,
        )
        self._append_locked(
            current_display_path,
            display_line,
        )
        self.health["written"] += 1

    def _append_locked(
        self,
        path: Path,
        line: str,
    ) -> None:
        lock = self._lock_for(path)
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="") as stream:
                stream.write(line)
                stream.flush()

    def _lock_for(self, path: Path) -> threading.Lock:
        with self._path_locks_guard:
            return self._path_locks.setdefault(path, threading.Lock())

    def _expire_seen(self, now: float) -> None:
        while self._seen:
            _, expires = next(iter(self._seen.items()))
            if expires > now:
                return
            self._seen.popitem(last=False)


def _display_message(document: dict[str, object]) -> str:
    if document["type"] == "DEBUG":
        return (
            f"[EVENT] - {document['event']} "
            f"value: {document['value']}"
        )
    truth = json.dumps(document["truth"], ensure_ascii=False)
    return (
        f"[INFERENCE] {document['status']}: {document['ADL']}, "
        f"sequence: {document['sequence']}, truth: {truth}, "
        f"missing: {document['missing']}"
    )
