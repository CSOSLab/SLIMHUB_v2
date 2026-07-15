from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.protocol.nus import normalize_mac


FINAL_INFERENCE_STATUSES = {"POP", "COMPLETE", "PARTIAL", "NO_MATCH"}


@dataclass(frozen=True)
class DataIngestBatch:
    adl_rows: list[tuple[object, ...]]
    inout_rows: list[tuple[object, ...]]
    offsets: dict[str, int]
    files: int
    lines: int


class DataDirectoryReader:
    """Incrementally convert legacy-compatible data files into DB rows."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        offsets: dict[str, object] | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.paths = paths
        self.offsets = {
            key: _as_nonnegative_int(value)
            for key, value in (offsets or {}).items()
        }
        self.environ = dict(os.environ if environ is None else environ)

    def read(self, *, house_mac: str) -> DataIngestBatch:
        adl_rows: list[tuple[object, ...]] = []
        inout_rows: list[tuple[object, ...]] = []
        updated_offsets = dict(self.offsets)
        files = 0
        lines = 0

        for path in self._discover_files():
            identity = _data_path_identity(self.paths.data_dir, path)
            if identity is None:
                continue
            location, mac, stream = identity
            key = str(path.resolve())
            start = self.offsets.get(key, 0)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if start > size:
                start = 0

            if stream == "rawdata":
                rows, end_offset, consumed = _read_rawdata(
                    path,
                    start,
                    house_mac=house_mac,
                    location=location,
                    mac=mac,
                )
                inout_rows.extend(rows)
            else:
                rows, end_offset, consumed = _read_debugstr(
                    path,
                    start,
                    house_mac=house_mac,
                    path_mac=mac,
                )
                adl_rows.extend(rows)
            updated_offsets[key] = end_offset
            files += 1
            lines += consumed

        return DataIngestBatch(
            adl_rows=adl_rows,
            inout_rows=inout_rows,
            offsets=updated_offsets,
            files=files,
            lines=lines,
        )

    def _discover_files(self) -> list[Path]:
        backfill = _as_bool(self.environ.get("SLIMHUB_DB_BACKFILL"))
        today = datetime.now().strftime("%Y-%m-%d")
        files: list[Path] = []
        for stream in ("rawdata", "debugstr"):
            pattern = f"*/*/*/inference/{stream}/*.txt"
            for path in self.paths.data_dir.glob(pattern):
                if backfill or path.stem == today:
                    files.append(path)
        return sorted(set(files))


def _read_rawdata(
    path: Path,
    start: int,
    *,
    house_mac: str,
    location: str,
    mac: str,
) -> tuple[list[tuple[object, ...]], int, int]:
    rows: list[tuple[object, ...]] = []
    consumed = 0
    with path.open("rb") as source:
        raw_header = source.readline()
        if not raw_header.endswith(b"\n"):
            return rows, start, consumed
        try:
            header = next(csv.reader([raw_header.decode("utf-8-sig")]))
        except (UnicodeDecodeError, csv.Error, StopIteration):
            return rows, start, consumed
        header_end = source.tell()
        source.seek(max(start, header_end))
        end_offset = source.tell()
        while True:
            line_start = source.tell()
            raw_line = source.readline()
            if not raw_line:
                break
            if not raw_line.endswith(b"\n"):
                source.seek(line_start)
                break
            end_offset = source.tell()
            consumed += 1
            try:
                values = next(csv.reader([raw_line.decode("utf-8-sig")]))
            except (UnicodeDecodeError, csv.Error, StopIteration):
                continue
            row = dict(zip(header, values))
            if str(row.get("GridEye") or "").strip() != "1":
                continue
            created_time = _timestamp_text(row.get("time"))
            direction = _optional_int(row.get("Direction"))
            if created_time is None or direction is None:
                continue
            rows.append(
                (house_mac, f"{location}:{mac}", created_time, direction)
            )
    return rows, end_offset, consumed


def _read_debugstr(
    path: Path,
    start: int,
    *,
    house_mac: str,
    path_mac: str,
) -> tuple[list[tuple[object, ...]], int, int]:
    rows: list[tuple[object, ...]] = []
    consumed = 0
    with path.open("rb") as source:
        source.seek(start)
        end_offset = start
        while True:
            line_start = source.tell()
            raw_line = source.readline()
            if not raw_line:
                break
            if not raw_line.endswith(b"\n"):
                source.seek(line_start)
                break
            end_offset = source.tell()
            consumed += 1
            try:
                document = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(document, dict):
                continue
            if str(document.get("type") or "").upper() != "INFERENCE":
                continue
            status = str(document.get("status") or "").upper()
            if status not in FINAL_INFERENCE_STATUSES:
                continue
            created_time = _timestamp_text(document.get("timestamp"))
            if created_time is None:
                continue
            device = str(document.get("device") or path_mac)
            try:
                device = normalize_mac(device)
            except ValueError:
                device = path_mac
            adl = str(document.get("ADL") or ("NO_MATCH" if status == "NO_MATCH" else ""))
            if not adl:
                continue
            rows.append(
                (
                    house_mac,
                    device,
                    created_time,
                    str(document.get("sequence") or ""),
                    adl,
                    _truth_value(document.get("truth")),
                )
            )
    return rows, end_offset, consumed


def _data_path_identity(
    data_dir: Path,
    path: Path,
) -> tuple[str, str, str] | None:
    try:
        relative = path.relative_to(data_dir)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) != 6 or parts[3] != "inference":
        return None
    location, _device_type, mac, _inference, stream, _filename = parts
    if stream not in {"rawdata", "debugstr"}:
        return None
    try:
        mac = normalize_mac(mac)
    except ValueError:
        return None
    return location, mac, stream


def _timestamp_text(value: object) -> str | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _truth_value(value: object) -> float | None:
    try:
        truth = float(str(value))
    except (TypeError, ValueError):
        return None
    return truth / 100 if truth > 1 else truth


def _optional_int(value: object) -> int | None:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return None


def _as_nonnegative_int(value: object) -> int:
    parsed = _optional_int(value)
    return max(parsed or 0, 0)


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
