from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from slimhub.config import DEFAULT_DEVICE_TYPE, AppPaths
from slimhub.events import ReportEvent
from slimhub.multimodal import MultimodalRecord
from slimhub.protocol.nus import normalize_mac


class DisplayWriter:
    """Persist legacy-compatible debug JSON and its concise operator view."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._prepared_paths: set[Path] = set()
        self._seen_event_keys: set[tuple[str, str, int, str]] = set()
        self._seen_activity_keys: set[str] = set()

    def ensure(self) -> None:
        """Make display paths observable as soon as the daemon starts."""
        self.paths.ensure()
        self.paths.display_dir.mkdir(parents=True, exist_ok=True)
        self.paths.display_path.touch(exist_ok=True)
        now = datetime.now()
        self._prepare_display_file(self.paths.display_path)
        self._prepare_display_file(
            self.paths.display_dir / f"{now.strftime('%Y-%m-%d')}.txt"
        )

    def write_inout(self, event: ReportEvent) -> None:
        fields = event.packet.fields
        if _is_v2_typed_inout(fields):
            # V2 emits the NCS JSON DEBUG record beside this typed report. Some
            # deployed builds omit the optional schema field but still carry
            # the stable boot/event/timestamp identity. The JSON record owns
            # the legacy display/debug timeline in either representation.
            return
        event_name = fields.get("event", "").upper()
        event_id = (fields.get("event_id") or fields.get("id") or "").upper()
        result = fields.get("result", "").upper()
        location = event.location or "undefined"
        if event_name == "SEQUENCE" and result == "ENTER_CONFIRMED" and event_id == "D0":
            debug = {"type": "DEBUG", "event": "ENTER", "value": 10}
        elif event_name == "SEQUENCE" and result == "EXIT_CONFIRMED" and event_id == "D1":
            debug = {"type": "DEBUG", "event": "EXIT", "value": 20}
        else:
            return
        key = (
            normalize_mac(event.mac),
            str(fields.get("boot_id") or fields.get("bid") or ""),
            _display_int(fields.get("event_ts_ms") or fields.get("ts")),
            str(debug["event"]),
        )
        if key in self._seen_event_keys:
            return
        self._seen_event_keys.add(key)
        self._write_debug_record(
            event.receipt_timestamp or event.timestamp,
            location=location,
            mac=event.mac,
            device_type=event.device_type,
            debug=debug,
        )

    def write_multimodal(self, record: MultimodalRecord) -> None:
        data = record.data
        kind = record.kind
        location = str(data.get("location") or "undefined")
        if kind == "legacy_event":
            key = (
                normalize_mac(record.mac),
                str(data.get("boot_id") or ""),
                _display_int(data.get("event_ts_ms")),
                str(data.get("event") or ""),
            )
            display = key not in self._seen_event_keys
            self._seen_event_keys.add(key)
            document = data.get("raw_document")
            if isinstance(document, dict):
                self._write_debug_record(
                    record.timestamp,
                    location=location,
                    mac=record.mac,
                    device_type=str(data.get("device_type") or DEFAULT_DEVICE_TYPE),
                    debug=dict(document),
                    display=display,
                )
            return
        if kind == "legacy_activity":
            activity_key = str(data.get("activity_key") or "")
            if activity_key in self._seen_activity_keys:
                return
            self._seen_activity_keys.add(activity_key)
            document = data.get("raw_document")
            if isinstance(document, dict):
                debug = dict(document)
                debug["truth_semantics"] = data.get("truth_semantics")
                self._write_debug_record(
                    record.timestamp,
                    location=location,
                    mac=record.mac,
                    device_type=str(data.get("device_type") or DEFAULT_DEVICE_TYPE),
                    debug=debug,
                )
            return
        if kind in {"adl_result", "derived_inference"}:
            if kind == "adl_result" and data.get("schema") == 2:
                # Schema 2 firmware emits the JSON activity timeline alongside
                # this richer typed detail. The JSON path owns UI display.
                return
            status = str(data.get("event") or "")
            if status not in {"PRE-DETECT", "POP", "COMPLETE", "PARTIAL", "NO_MATCH"}:
                return
            self._write_debug_record(
                record.timestamp,
                location=location,
                mac=record.mac,
                device_type=str(data.get("device_type") or DEFAULT_DEVICE_TYPE),
                debug={
                    "type": "INFERENCE",
                    "status": status,
                    "ADL": data.get("adl") or "NO_MATCH",
                    "sequence": data.get("sequence") or "N/A",
                    "truth": data.get("truth"),
                    "missing": data.get("missing"),
                    "derived": bool(data.get("derived")),
                    "truth_semantics": data.get("truth_semantics"),
                },
            )

    def _write_debug_record(
        self,
        timestamp: float,
        *,
        location: str,
        mac: str,
        device_type: str,
        debug: dict[str, object],
        display: bool = True,
    ) -> None:
        time_value = datetime.fromtimestamp(timestamp)
        document = dict(debug)
        reported_device = document.get("device")
        document["device"] = normalize_mac(mac)
        if reported_device and str(reported_device) != normalize_mac(mac):
            document["reported_device"] = reported_device
        document["timestamp"] = time_value.strftime("%Y-%m-%d %H:%M:%S")
        debug_path = (
            self.paths.data_dir
            / location
            / (device_type or DEFAULT_DEVICE_TYPE)
            / mac
            / "inference"
            / "debugstr"
            / f"{time_value.strftime('%Y-%m-%d')}.txt"
        )
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with debug_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(document, ensure_ascii=False) + "\n")

        if not display:
            return
        message = _format_debug(document)
        if message is not None:
            self._append(timestamp, f"{location} {message}")

    def _append(self, timestamp: float, message: str) -> None:
        time_value = datetime.fromtimestamp(timestamp)
        line = f"{time_value.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n"
        self.ensure()
        daily_path = self.paths.display_dir / f"{time_value.strftime('%Y-%m-%d')}.txt"
        self._prepare_display_file(daily_path)
        for path in (daily_path, self.paths.display_path):
            with path.open("a", encoding="utf-8") as f:
                f.write(line)

    def _prepare_display_file(self, path: Path) -> None:
        if path in self._prepared_paths:
            return
        self._prepared_paths.add(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except FileNotFoundError:
            path.touch()
            return
        retained = [line for line in lines if _is_operator_line(line)]
        if retained != lines:
            path.write_text("".join(retained), encoding="utf-8")


def _display_truth(value: object) -> str:
    try:
        truth = float(str(value))
    except (TypeError, ValueError):
        return "N/A"
    if truth > 1:
        truth /= 100
    return f"{truth:.2f}"


def _format_debug(debug: dict[str, object]) -> str | None:
    if debug.get("type") in {"DEBUG", "EVENT"} and debug.get("event") == "ENTER":
        return f"[EVENT] - ENTER value: {debug.get('value', 10)}"
    if debug.get("type") in {"DEBUG", "EVENT"} and debug.get("event") == "EXIT":
        return f"[EVENT] - EXIT value: {debug.get('value', 20)}"
    if debug.get("type") != "INFERENCE":
        return None
    missing = debug.get("missing")
    truth_semantics = str(debug.get("truth_semantics") or "")
    if missing in (None, 0, "0") and truth_semantics != "adaptive_score_ratio":
        missing = ""
    truth_label = _display_truth(debug.get("truth"))
    if truth_semantics == "adaptive_score_ratio":
        truth_label += " (adaptive)"
    elif truth_semantics == "legacy_heap_truth":
        truth_label += " (legacy)"
    return (
        f"[INFERENCE] {debug.get('status', '')}: {debug.get('ADL', 'N/A')}, "
        f"sequence: {debug.get('sequence') or 'N/A'}, "
        f"truth: {truth_label}, missing: {missing}"
    )


def _display_int(value: object) -> int:
    try:
        return int(str(value), 10)
    except (TypeError, ValueError):
        return -1


def _is_v2_typed_inout(fields: dict[str, str]) -> bool:
    if _display_int(fields.get("schema")) == 2:
        return True
    return (
        _has_display_value(fields, "bid", "boot_id")
        and _has_display_value(fields, "cid", "event_seq")
        and _has_display_value(fields, "timestamp", "event_ts_ms", "ts")
    )


def _has_display_value(fields: dict[str, str], *names: str) -> bool:
    return any(str(fields.get(name) or "").strip() for name in names)


def _is_operator_line(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            " [EVENT] - ENTER value:",
            " [EVENT] - EXIT value:",
            " [INFERENCE] ",
        )
    )
