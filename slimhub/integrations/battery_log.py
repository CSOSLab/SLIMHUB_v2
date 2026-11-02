from __future__ import annotations

import json
import struct
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from slimhub.config import AppPaths


# 신규 코드(2026-11-02): Node 배터리 측정(VLB1)을 debugstr과 같은 JSON lines로 씁니다.
# daemon은 측정 원본을 data/validation/<MAC12>/<stream>.jsonl에 저장합니다. 이 단계는
# 새로 받은 측정만 data/<LOC>/DEAN_NODE_V2/<MAC>/inference/battery/<날짜>.txt 끝에
# 덧붙이므로(append-only) DB ingest가 debugstr처럼 byte offset으로 읽을 수 있습니다.
SEOUL = ZoneInfo("Asia/Seoul")
DEVICE_TYPE = "DEAN_NODE_V2"
ROOM_LOCATIONS = ("ENTRY", "LIVING", "BEDROOM", "KITCHEN", "TOILET")
DEFAULT_SINCE = "2026-10-01"

# Node firmware dean_usd_log.c s_soc_table (임시 표, 2026-10-25 유선 Node 방전 기록).
SOC_TABLE = (
    (4180, 100), (4131, 95), (4083, 90), (4051, 85), (4021, 80),
    (3979, 75), (3939, 70), (3903, 65), (3863, 60), (3801, 55),
    (3743, 50), (3707, 45), (3667, 40), (3643, 35), (3600, 30),
    (3569, 25), (3544, 20), (3500, 15), (3455, 10), (3393, 5),
    (3000, 0),
)
# Firmware dean_validation_battery_level_usable(): HIGH_RANGE/VOLTAGE_RANGE는 허용.
INVALID_FLAGS = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x80 | 0x100
ADC_READS = 12
RECORD_BYTES = 76
NOT_BATTERY = -1


def battery_export_state_path(paths: AppPaths) -> Path:
    return paths.db_sync_dir / "battery_export_state.json"


def percent_from_mv(mv: int) -> int:
    """Same integer interpolation as the firmware usd_batt_percent_from_mv()."""
    if mv <= 0:
        return 0
    if mv >= SOC_TABLE[0][0]:
        return SOC_TABLE[0][1]
    for (high_mv, high_pct), (low_mv, low_pct) in zip(SOC_TABLE, SOC_TABLE[1:]):
        if mv >= low_mv:
            mv_span = high_mv - low_mv
            pct_span = high_pct - low_pct
            return low_pct + ((mv - low_mv) * pct_span + mv_span // 2) // mv_span
    return 0


def decode_battery_record(data: bytes) -> dict[str, int] | None:
    """Decode one DBAT record (firmware dean_validation_battery.h)."""
    if len(data) != RECORD_BYTES or data[:4] != b"DBAT":
        return None
    stream, seq = struct.unpack_from("<QQ", data, 8)
    payload = 24
    (usb_pin,) = struct.unpack_from("<b", data, payload + 2)
    (flags,) = struct.unpack_from("<I", data, payload + 4)
    pin_mv, vbat_mv = struct.unpack_from("<ii", data, payload + 16)
    (uptime_ms,) = struct.unpack_from("<Q", data, payload + 24)
    return {
        "stream": stream,
        "seq": seq,
        "count": data[payload + 1],
        "usb_pin": usb_pin,
        "flags": flags,
        "pin_mv": pin_mv,
        "vbat_mv": vbat_mv,
        "uptime_ms": uptime_ms,
    }


def battery_line(mac: str, record: dict[str, int], measured_ms: float) -> tuple[str, str]:
    """Return (date, JSON line) for one measurement; timestamp = measurement time."""
    measured = datetime.fromtimestamp(measured_ms / 1000.0, SEOUL)
    valid = (
        record["count"] == ADC_READS
        and record["pin_mv"] >= 0
        and record["vbat_mv"] >= 0
        and (record["flags"] & INVALID_FLAGS) == 0
    )
    document = {
        "device": mac,
        "type": "BATTERY",
        # 무효 측정은 percentage가 null입니다(batt_mv는 남김).
        "percentage": percent_from_mv(record["vbat_mv"]) if valid else None,
        "batt_mv": record["vbat_mv"] if record["vbat_mv"] >= 0 else None,
        # USB 검출 핀 LOW = USB 연결(usb 1).
        "usb": {0: 1, 1: 0}.get(record["usb_pin"]),
        "uptime_s": record["uptime_ms"] // 1000,
        "timestamp": measured.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return measured.strftime("%Y-%m-%d"), json.dumps(document, ensure_ascii=False)


def export_battery_logs(paths: AppPaths, *, since: str = DEFAULT_SINCE) -> dict[str, object]:
    """Append newly received VLB1 measurements to inference/battery/<date>.txt.

    Without a state file every measurement since `since` is written and the
    day files are replaced (first run); afterwards only new ones are appended.
    The state keeps, per validation file, the byte offset already read, and per
    battery stream (one Node boot) the last written seq and the boot time
    estimate min(receipt - uptime).
    """
    state_path = battery_export_state_path(paths)
    state = _read_json(state_path)
    rebuild = not state
    files: dict[str, int] = dict(state.get("files") or {})
    streams: dict[str, dict[str, float]] = dict(state.get("streams") or {})

    new_records: list[tuple[str, str, dict[str, int]]] = []
    for path in sorted((paths.data_dir / "validation").glob("*/*.jsonl")):
        key = str(path)
        start = int(files.get(key, 0))
        if start == NOT_BATTERY:
            continue
        rows, end = _read_complete_lines(path, start)
        if start == 0 and rows and rows[0].get("format") != "VLB1":
            files[key] = NOT_BATTERY  # 사건·원본 기록 stream(배터리 stream과 분리됨)
            continue
        files[key] = end
        for row in rows:
            if row.get("format") != "VLB1":
                continue
            mac = str(row.get("mac") or "").upper()
            try:
                received_ms = float(row["receipt_ts"]) * 1000.0
            except (KeyError, TypeError, ValueError):
                continue
            for item in (row.get("decoded") or {}).get("records") or []:
                try:
                    record = decode_battery_record(bytes.fromhex(str(item.get("data") or "")))
                except ValueError:
                    record = None
                if record is None:
                    continue
                stream_key = f"{mac}/{record['stream']:016x}"
                stream = streams.setdefault(stream_key, {"seq": 0})
                boot_ms = received_ms - record["uptime_ms"]
                stream["boot_ms"] = min(stream.get("boot_ms", boot_ms), boot_ms)
                if record["seq"] <= stream["seq"]:
                    continue  # 이미 쓴 측정(재전송)
                stream["seq"] = record["seq"]
                new_records.append((mac, stream_key, record))

    outputs: dict[Path, list[tuple[float, str]]] = defaultdict(list)
    skipped = 0
    for mac, stream_key, record in new_records:
        measured_ms = streams[stream_key]["boot_ms"] + record["uptime_ms"]
        date, line = battery_line(mac, record, measured_ms)
        location = _location_for(paths, mac, date)
        if date < since or location is None:
            skipped += 1
            continue
        path = (
            paths.data_dir / location / DEVICE_TYPE / mac / "inference" / "battery" / f"{date}.txt"
        )
        outputs[path].append((measured_ms, line))

    for path, lines in outputs.items():
        lines.sort()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(line + "\n" for _, line in lines)
        if rebuild:
            temporary = path.with_suffix(".txt.tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        else:
            with path.open("a", encoding="utf-8") as output:
                output.write(text)

    _write_json_atomic(state_path, {"files": files, "streams": streams})
    return {
        "rebuild": rebuild,
        "written": sum(len(lines) for lines in outputs.values()),
        "files": len(outputs),
        "skipped": skipped,
    }


def _location_for(paths: AppPaths, mac: str, date: str) -> str | None:
    """The room folder that has this Node's debugstr/rawdata for date, else its config."""
    found = [
        room
        for room in ROOM_LOCATIONS
        if any(
            (paths.data_dir / room / DEVICE_TYPE / mac / "inference" / stream / f"{date}.txt").exists()
            for stream in ("debugstr", "rawdata")
        )
    ]
    if len(found) == 1:
        return found[0]
    location = _read_json(paths.config_dir / f"{mac}.json").get("location")
    return location if location in ROOM_LOCATIONS else None


def _read_complete_lines(path: Path, start: int) -> tuple[list[dict[str, object]], int]:
    rows: list[dict[str, object]] = []
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return rows, start
    if start > size:
        start = 0
    with path.open("rb") as source:
        source.seek(start)
        end = start
        for raw_line in source:
            if not raw_line.endswith(b"\n"):
                break
            end += len(raw_line)
            try:
                row = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows, end


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
