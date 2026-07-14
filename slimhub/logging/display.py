from __future__ import annotations

from datetime import datetime

from slimhub.config import AppPaths
from slimhub.events import ReportEvent
from slimhub.multimodal import MultimodalRecord


class DisplayWriter:
    """Write concise operator-facing events without treating the display as data storage."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def ensure(self) -> None:
        """Make display paths observable as soon as the daemon starts."""
        self.paths.ensure()
        self.paths.display_dir.mkdir(parents=True, exist_ok=True)
        self.paths.display_path.touch(exist_ok=True)

    def write_inout(self, event: ReportEvent) -> None:
        fields = event.packet.fields
        event_name = fields.get("event", "").upper()
        event_id = (fields.get("event_id") or fields.get("id") or "").upper()
        result = fields.get("result", "").upper()
        location = event.location or "undefined"
        if event_name == "SEQUENCE" and result == "ENTER_CONFIRMED" and event_id == "D0":
            message = "[EVENT] - ENTER value: 10"
        elif event_name == "SEQUENCE" and result == "EXIT_CONFIRMED" and event_id == "D1":
            message = "[EVENT] - EXIT value: 20"
        else:
            return
        self._append(event.receipt_timestamp or event.timestamp, f"{location} {message}")

    def write_multimodal(self, record: MultimodalRecord) -> None:
        data = record.data
        kind = record.kind
        location = str(data.get("location") or "undefined")
        if kind == "feature":
            name = str(data.get("event") or "")
            if name == "ENV":
                label = data.get("canonical_name") or "N/A"
                message = f"[EVENT] - '{label}' event was detected"
            elif name == "SOUND":
                label = str(data.get("label") or "unknown")
                if label.strip().lower() in {"background", "unknown"}:
                    return
                message = f"[EVENT] - Sound '{label}' was detected"
            else:
                return
            self._append(record.timestamp, f"{location} {message}")
        elif kind == "adl_result":
            status = str(data.get("event") or "")
            adl = data.get("adl") or "NO_MATCH"
            sequence = data.get("sequence") or "N/A"
            truth = _display_truth(data.get("truth"))
            missing = data.get("missing")
            message = (
                f"{location} [INFERENCE] {status}: {adl}, sequence: {sequence}, "
                f"truth: {truth}, missing: {missing if missing is not None else 'None'}"
            )
            self._append(record.timestamp, message)

    def _append(self, timestamp: float, message: str) -> None:
        time_value = datetime.fromtimestamp(timestamp)
        line = f"{time_value.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n"
        self.ensure()
        daily_path = self.paths.display_dir / f"{time_value.strftime('%Y-%m-%d')}.txt"
        for path in (daily_path, self.paths.display_path):
            with path.open("a", encoding="utf-8") as f:
                f.write(line)


def _display_truth(value: object) -> str:
    try:
        truth = float(str(value))
    except (TypeError, ValueError):
        return "N/A"
    if truth > 1:
        truth /= 100
    return f"{truth:.2f}"
