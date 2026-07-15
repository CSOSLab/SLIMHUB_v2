from __future__ import annotations

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
            location, mac = identity
            key = str(path.resolve())
            start = self.offsets.get(key, 0)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if start > size:
                start = 0

            # 변경 사항: DB 입력 원천을 debugstr 하나로 통일했습니다.
            # IN/OUT은 rawdata의 GridEye/Direction을 추정값으로 사용하지 않고,
            # Node가 확정해 보낸 EVENT ENTER=10 / EXIT=20만 사용합니다.
            adl, inout, end_offset, consumed = _read_debugstr(
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
        backfill = _as_bool(self.environ.get("SLIMHUB_DB_BACKFILL"))
        today = datetime.now().strftime("%Y-%m-%d")
        files: list[Path] = []
        # 변경 사항: DB updater는 debugstr만 읽습니다. rawdata는 수집 원본으로
        # 계속 보존하지만 local DB ingest source에는 포함하지 않습니다.
        pattern = "*/*/*/inference/debugstr/*.txt"
        for path in self.paths.data_dir.glob(pattern):
            if backfill or path.stem == today:
                files.append(path)
        return sorted(set(files))


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
            created_time = _timestamp_text(document.get("timestamp"))
            if created_time is None:
                continue
            record_type = str(document.get("type") or "").upper()

            # 변경 사항: debugstr의 확정 EVENT만 IN/OUT DB row로 변환합니다.
            # DEBUG record는 같은 EVENT의 legacy duplicate일 수 있어 제외합니다.
            if record_type == "EVENT":
                event = str(document.get("event") or "").upper()
                value = _optional_int(document.get("value"))
                expected_value = {"ENTER": 10, "EXIT": 20}.get(event)
                if expected_value is not None and value == expected_value:
                    inout_rows.append(
                        (
                            house_mac,
                            f"{location}:{path_mac}",
                            created_time,
                            expected_value,
                        )
                    )
                continue

            if record_type != "INFERENCE":
                continue
            status = str(document.get("status") or "").upper()
            if status not in FINAL_INFERENCE_STATUSES:
                continue
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
) -> tuple[str, str] | None:
    try:
        relative = path.relative_to(data_dir)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) != 6 or parts[3] != "inference":
        return None
    location, _device_type, mac, _inference, stream, _filename = parts
    if stream != "debugstr":
        return None
    try:
        mac = normalize_mac(mac)
    except ValueError:
        return None
    return location, mac


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
