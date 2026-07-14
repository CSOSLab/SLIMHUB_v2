from __future__ import annotations

from datetime import datetime

from slimhub.config import AppPaths
from slimhub.events import ReportEvent
from slimhub.multimodal import MultimodalRecord


class DisplayWriter:
    """Write concise operator-facing events without treating the display as data storage."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def write_inout(self, event: ReportEvent) -> None:
        fields = event.packet.fields
        event_name = fields.get("event", "").upper()
        event_id = (fields.get("event_id") or fields.get("id") or "").upper()
        result = fields.get("result", "").upper()
        location = event.location or "undefined"
        if event_name == "ENTER" and fields.get("code") == "10":
            message = "ENTER candidate (RAW10 sidecar)"
        elif event_name == "SEQUENCE" and result:
            message = f"{result} {event_id}".strip()
        elif event_name == "EVENT" and event_id in {"C0", "C1"}:
            message = f"COMMAND ACK {event_id} occupied={fields.get('occupied', '?')}"
        else:
            return
        self._append(event.receipt_timestamp or event.timestamp, f"{location} [INOUT] {message}")

    def write_multimodal(self, record: MultimodalRecord) -> None:
        data = record.data
        kind = record.kind
        location = str(data.get("location") or "undefined")
        if kind == "feature":
            name = str(data.get("event") or "")
            if name == "ENV":
                message = (
                    f"ENV {data.get('event_id', '?')} "
                    f"({data.get('canonical_name') or 'unknown'}) confidence={data.get('confidence', '?')}"
                )
            elif name == "SOUND":
                message = (
                    f"SOUND {data.get('event_id', '?')} "
                    f"({data.get('label') or 'unknown'}) count={data.get('count', '?')}"
                )
            else:
                return
            self._append(record.timestamp, f"{location} [EVENT] {message}")
        elif kind == "adl_result":
            event_name = str(data.get("event") or "")
            qualifier = "provisional" if data.get("provisional") else "final"
            message = (
                f"ADL {qualifier} {event_name}: {data.get('adl') or 'NO_MATCH'} "
                f"truth={data.get('truth', '?')}"
            )
            self._append(record.timestamp, f"{location} [ADL] {message}")
        elif kind == "baseline":
            self._append(
                record.timestamp,
                f"{location} [BASELINE] {data.get('status') or '?'} "
                f"ready={data.get('ready_mask') or '?'}",
            )

    def _append(self, timestamp: float, message: str) -> None:
        time_value = datetime.fromtimestamp(timestamp)
        line = f"{time_value.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n"
        self.paths.ensure()
        self.paths.display_dir.mkdir(parents=True, exist_ok=True)
        daily_path = self.paths.display_dir / f"{time_value.strftime('%Y-%m-%d')}.txt"
        for path in (daily_path, self.paths.display_path):
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
