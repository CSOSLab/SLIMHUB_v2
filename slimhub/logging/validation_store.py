"""Durable store for DEAN Node validation LOGDATA.

One JSONL file per Node and stream under ``data/validation/<MAC>/<stream>.jsonl``.
Each frame is appended and fsync()ed before the daemon sends ``validation_ack``,
so an ACK always means the bytes are on disk. Re-sent frames (same key) are not
appended twice. VLS1 status frames are appended without de-duplication.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.protocol.nus import LogDataPacket


def _jsonable(value: object) -> object:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def record_key(packet: LogDataPacket) -> str | None:
    value = packet.value or {}
    fmt = packet.format
    if fmt == "VLE1":
        return f"event:{packet.bundle_id}"
    if fmt == "VLR1":
        return (f"range:{packet.bundle_id}:{value[first_seq]}:{value[last_seq]}:"
                f"{value[unavailable]}:{value[dropped]}")
    if fmt == "VLM1":
        return f"meta:{value[metadata_id]}:{value[kind]}:{value[offset]}"
    if fmt == "VLB1":
        seqs = [r["seq"] for r in value["records"]]
        return f"battery:{seqs[0]}-{seqs[-1]}"
    if value.get("records"):
        seqs = [r["seq"] for r in value["records"]]
        return f"log:{seqs[0]}-{seqs[-1]}"
    return None  # VLS1 status


class ValidationStore:
    def __init__(self, paths: AppPaths) -> None:
        self.root = paths.data_dir / "validation"
        self._seen: dict[Path, set[str]] = {}
        self._lock = threading.Lock()

    def path_for(self, mac: str, stream_id: int) -> Path:
        return self.root / mac.replace(":", "").upper() / f"{stream_id:016x}.jsonl"

    def _load_seen(self, path: Path) -> set[str]:
        seen = self._seen.get(path)
        if seen is None:
            seen = set()
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            key = json.loads(line).get("key")
                        except json.JSONDecodeError:
                            continue
                        if key:
                            seen.add(key)
            self._seen[path] = seen
        return seen

    def store(self, mac: str, packet: LogDataPacket, payload: bytes, received_at: float) -> bool:
        """Append durably. Returns False when the frame was already stored."""
        if packet.value is None or not packet.stream_id:
            raise ValueError("only decoded LOGDATA can be stored")
        path = self.path_for(mac, packet.stream_id)
        key = record_key(packet)
        with self._lock:
            seen = self._load_seen(path)
            if key is not None and key in seen:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "receipt_ts": received_at,
                "mac": mac,
                "format": packet.format,
                "stream": f"{packet.stream_id:016x}",
                "bundle": packet.bundle_id,
                "key": key,
                "payload_hex": payload.hex(),
                "decoded": _jsonable(packet.value),
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if key is not None:
                seen.add(key)
        return True
