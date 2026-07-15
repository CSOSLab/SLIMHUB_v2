from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from slimhub.config import DEFAULT_DEVICE_TYPE, AppPaths
from slimhub.events import ReportEvent
from slimhub.multimodal import MultimodalRecord


class DisplayWriter:
    """Persist legacy-compatible debug JSON and its concise operator view."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._prepared_paths: set[Path] = set()

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
        if kind in {"adl_result", "derived_inference"}:
            status = str(data.get("event") or "")
            if status not in {"COMPLETE", "PARTIAL", "NO_MATCH"}:
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
    ) -> None:
        time_value = datetime.fromtimestamp(timestamp)
        document = {
            "device": mac,
            **debug,
            "timestamp": time_value.strftime("%Y-%m-%d %H:%M:%S"),
        }
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
    if debug.get("type") == "DEBUG" and debug.get("event") == "ENTER":
        return f"[EVENT] - ENTER value: {debug.get('value', 10)}"
    if debug.get("type") == "DEBUG" and debug.get("event") == "EXIT":
        return f"[EVENT] - EXIT value: {debug.get('value', 20)}"
    if debug.get("type") != "INFERENCE":
        return None
    missing = debug.get("missing")
    if missing in (None, 0, "0"):
        missing = ""
    return (
        f"[INFERENCE] {debug.get('status', '')}: {debug.get('ADL', 'N/A')}, "
        f"sequence: {debug.get('sequence') or 'N/A'}, "
        f"truth: {_display_truth(debug.get('truth'))}, missing: {missing}"
    )


def _is_operator_line(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            " [EVENT] - ENTER value:",
            " [EVENT] - EXIT value:",
            " [INFERENCE] ",
        )
    )
