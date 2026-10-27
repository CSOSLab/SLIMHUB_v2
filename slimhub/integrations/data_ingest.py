from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.protocol.nus import normalize_mac


# Unit-space folders written by the daemon. Manual copies (_BEDROOM),
# undefined, display and validation trees are not DB sources.
ROOM_LOCATIONS = {"ENTRY", "LIVING", "BEDROOM", "KITCHEN", "TOILET"}
INOUT_DIRECTIONS = {10, 20}


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
            if start == size:
                continue

            # 변경 사항(2026-10-27): ESA 결과는 debugstr INFERENCE 전체(status 무관),
            # IN/OUT은 v1과 같이 rawdata의 GridEye=1, Direction=10/20 행에서 읽습니다.
            # debugstr의 DEBUG/EVENT 행은 연결이 끊기면 빠지므로 IN/OUT 원천으로 쓰지 않습니다.
            reader = _read_debugstr if stream == "debugstr" else _read_rawdata
            adl, inout, end_offset, consumed = reader(
                path,
                start,
                house_mac=house_mac,
                location=location,
                path_mac=mac,
            )
            adl_rows.extend(adl)
            inout_rows.extend(inout)
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
        # 변경 사항(2026-10-27): 오늘 파일만 읽으면 자정 직전 행과 늦게 다시 쓰인 행을
        # 놓칩니다. 모든 날짜 파일을 보고, 다 읽은 파일은 offset으로 건너뜁니다.
        files: list[Path] = []
        for stream in ("debugstr", "rawdata"):
            files.extend(self.paths.data_dir.glob(f"*/*/*/inference/{stream}/*.txt"))
        return sorted(set(files))


def _line_start(source, start: int) -> int:
    """Return start, or the next line start if start falls inside a line."""
    if start <= 0:
        return 0
    source.seek(start - 1)
    if source.read(1) == b"\n":
        return start
    source.readline()
    return source.tell()


def _read_rawdata(
    path: Path,
    start: int,
    *,
    house_mac: str,
    location: str,
    path_mac: str,
) -> tuple[
    list[tuple[object, ...]],
    list[tuple[object, ...]],
    int,
    int,
]:
    inout_rows: list[tuple[object, ...]] = []
    consumed = 0
    with path.open("rb") as source:
        header_line = source.readline()
        if not header_line.endswith(b"\n"):
            return [], [], 0, 0
        # Columns by name: v1/June files have 22 columns, July+ files 26.
        header = next(csv.reader([header_line.decode("utf-8", "replace").strip()]))
        columns = {name.strip(): index for index, name in enumerate(header)}
        if not {"time", "GridEye", "Direction"} <= columns.keys():
            return [], [], source.tell(), 1
        end_offset = max(_line_start(source, start), source.tell())
        source.seek(end_offset)
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
                row = next(csv.reader([raw_line.decode("utf-8").strip()]))
            except (UnicodeDecodeError, StopIteration, csv.Error):
                continue
            if len(row) <= max(columns["time"], columns["GridEye"], columns["Direction"]):
                continue
            if row[columns["GridEye"]].strip() != "1":
                continue
            direction = _optional_int(row[columns["Direction"]])
            created_time = _timestamp_text(row[columns["time"]])
            if direction not in INOUT_DIRECTIONS or created_time is None:
                continue
            inout_rows.append(
                (house_mac, f"{location}:{path_mac}", created_time, direction)
            )
    return [], inout_rows, end_offset, consumed


def _read_debugstr(
    path: Path,
    start: int,
    *,
    house_mac: str,
    location: str,
    path_mac: str,
) -> tuple[
    list[tuple[object, ...]],
    list[tuple[object, ...]],
    int,
    int,
]:
    adl_rows: list[tuple[object, ...]] = []
    inout_rows: list[tuple[object, ...]] = []
    consumed = 0
    with path.open("rb") as source:
        end_offset = _line_start(source, start)
        source.seek(end_offset)
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
            created_time = _timestamp_text(document.get("timestamp"))
            if created_time is None:
                continue
            record_type = str(document.get("type") or "").upper()
            # 모든 ESA 결과(COMPLETE, PARTIAL, NO_MATCH, POP, PRE-DETECT 등)를 넣습니다.
            if record_type != "INFERENCE":
                continue
            status = str(document.get("status") or "").upper()
            device = str(document.get("device") or path_mac)
            try:
                device = normalize_mac(device)
            except ValueError:
                device = path_mac
            adl = str(document.get("ADL") or ("NO_MATCH" if status == "NO_MATCH" else ""))
            if not adl:
                continue
            adl_rows.append(
                (
                    house_mac,
                    device,
                    created_time,
                    str(document.get("sequence") or ""),
                    adl,
                    _truth_value(document.get("truth")),
                )
            )
    return adl_rows, inout_rows, end_offset, consumed


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
    if location not in ROOM_LOCATIONS or stream not in {"debugstr", "rawdata"}:
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
