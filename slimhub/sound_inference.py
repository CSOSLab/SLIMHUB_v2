from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from slimhub.events import ReportEvent
from slimhub.protocol.nus import normalize_mac


SCHEMA_VERSION = 1
LOCATIONS = frozenset({"ENTRY", "LIVING", "BEDROOM", "KITCHEN", "TOILET"})
SOURCES = frozenset({"tflm", "rms_gate"})
REQUIRED_FIELDS = (
    "schema",
    "bid",
    "location",
    "class_index",
    "class_count",
    "label",
    "semantic",
    "confidence",
    "model",
    "source",
    "ts",
    "duration_ms",
)
HEX8 = re.compile(r"^[0-9a-fA-F]{8}$")
UINT32_MAX = (1 << 32) - 1


class NodeSoundMetadata(Protocol):
    location: str | None
    class_count: int | None
    model: str | None
    semantic: str | None
    config: str | None
    last_reason: str | None


@dataclass(frozen=True)
class SoundInference:
    node_mac: str
    boot_id: str
    received_at: float
    node_timestamp_ms: int
    duration_ms: int
    location: str
    class_index: int
    class_count: int
    label: str
    semantic: str
    confidence: float
    model_short_sha: str
    source: str
    rms: float | None
    estimated_db_spl: float | None
    adl_eligible: bool
    metadata_consistent: bool


@dataclass(frozen=True)
class SoundDiagnostic:
    kind: str
    reason: str


@dataclass(frozen=True)
class SoundInferenceOutcome:
    inference: SoundInference | None
    stored: bool
    duplicate: bool
    diagnostics: tuple[SoundDiagnostic, ...]


class SoundInferenceValidationError(ValueError):
    pass


class SoundInferenceStore:
    """Lossless, Node-authoritative SOUND/INFERENCE v2 storage."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        try:
            self._migrate()
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __del__(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _migrate(self) -> None:
        # The dedicated database may already contain older or unrelated tables.
        # DDL is transactional and never drops or rewrites them.
        self._connection.execute("BEGIN IMMEDIATE")
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sound_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sound_inference_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_mac TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    node_timestamp_ms INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    class_index INTEGER NOT NULL,
                    class_count INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    semantic TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    model_short_sha TEXT NOT NULL,
                    source TEXT NOT NULL,
                    rms REAL,
                    estimated_db_spl REAL,
                    adl_eligible INTEGER NOT NULL,
                    metadata_consistent INTEGER NOT NULL,
                    raw_payload TEXT NOT NULL,
                    UNIQUE (
                        node_mac, boot_id, node_timestamp_ms, duration_ms,
                        location, model_short_sha, class_index, source
                    )
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sound_catalog_v2 (
                    node_mac TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    model_short_sha TEXT NOT NULL,
                    location TEXT NOT NULL,
                    class_index INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    class_count INTEGER NOT NULL,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    observations INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (
                        node_mac, boot_id, model_short_sha, location, class_index
                    )
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sound_session_v2 (
                    node_mac TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    model_short_sha TEXT NOT NULL,
                    location TEXT NOT NULL,
                    report_class_count INTEGER NOT NULL,
                    report_semantic TEXT NOT NULL,
                    node_location TEXT,
                    node_class_count INTEGER,
                    node_model TEXT,
                    node_semantic TEXT,
                    node_config TEXT,
                    node_last_reason TEXT,
                    metadata_consistent INTEGER NOT NULL,
                    last_seen_at REAL NOT NULL,
                    PRIMARY KEY (
                        node_mac, boot_id, model_short_sha, location
                    )
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sound_diagnostic_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at REAL NOT NULL,
                    node_mac TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    raw_payload TEXT NOT NULL
                )
                """
            )
            existing_version = self._connection.execute(
                "SELECT value FROM sound_store_meta WHERE key='schema_version'"
            ).fetchone()
            if (
                existing_version is not None
                and int(existing_version["value"]) > SCHEMA_VERSION
            ):
                raise RuntimeError(
                    "sound inference database schema is newer than this SLIMHUB build"
                )
            self._connection.execute(
                """
                INSERT INTO sound_store_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def handle_report(
        self,
        event: ReportEvent,
        node: NodeSoundMetadata | None = None,
    ) -> SoundInferenceOutcome:
        raw = event.payload.decode("utf-8", errors="replace")
        try:
            inference = parse_sound_inference(event)
        except SoundInferenceValidationError as exc:
            diagnostic = SoundDiagnostic("sound_inference_rejected", str(exc))
            self._write_diagnostics(event.timestamp, event.mac, raw, (diagnostic,))
            return SoundInferenceOutcome(None, False, False, (diagnostic,))

        diagnostics = list(_protocol_diagnostics(inference))
        diagnostics.extend(_metadata_diagnostics(inference, node))
        inference = replace(
            inference,
            adl_eligible=(
                inference.semantic.strip().lower() != "unknown"
                and not any(
                    item.kind
                    in {"sound_metadata_mismatch", "sound_protocol_mismatch"}
                    for item in diagnostics
                )
            ),
            metadata_consistent=not any(
                item.kind == "sound_metadata_mismatch"
                for item in diagnostics
            ),
        )

        with self._connection:
            catalog_diagnostics = self._catalog_diagnostics(inference)
            diagnostics.extend(catalog_diagnostics)
            if catalog_diagnostics:
                inference = replace(
                    inference,
                    adl_eligible=False,
                    metadata_consistent=False,
                )
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO sound_inference_v2 (
                    node_mac, boot_id, received_at, node_timestamp_ms,
                    duration_ms, location, class_index, class_count, label,
                    semantic, confidence, model_short_sha, source, rms,
                    estimated_db_spl, adl_eligible, metadata_consistent,
                    raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inference.node_mac,
                    inference.boot_id,
                    inference.received_at,
                    inference.node_timestamp_ms,
                    inference.duration_ms,
                    inference.location,
                    inference.class_index,
                    inference.class_count,
                    inference.label,
                    inference.semantic,
                    inference.confidence,
                    inference.model_short_sha,
                    inference.source,
                    inference.rms,
                    inference.estimated_db_spl,
                    int(inference.adl_eligible),
                    int(inference.metadata_consistent),
                    raw,
                ),
            )
            stored = cursor.rowcount == 1
            if stored and not catalog_diagnostics:
                self._upsert_catalog(inference)
            self._upsert_session(inference, node)
            self._write_diagnostics(
                event.timestamp,
                event.mac,
                raw,
                tuple(diagnostics),
                in_transaction=True,
            )
        return SoundInferenceOutcome(
            inference,
            stored,
            not stored,
            tuple(diagnostics),
        )

    def snapshot(self, address: str | None = None) -> list[dict[str, object]]:
        parameters: tuple[object, ...] = ()
        where = ""
        if address is not None:
            where = "WHERE node_mac = ?"
            parameters = (normalize_mac(address),)
        latest_rows = self._connection.execute(
            f"""
            SELECT inference.*
            FROM sound_inference_v2 AS inference
            JOIN (
                SELECT node_mac, MAX(id) AS id
                FROM sound_inference_v2
                {where}
                GROUP BY node_mac
            ) AS latest ON latest.id = inference.id
            ORDER BY inference.node_mac
            """,
            parameters,
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in latest_rows:
            catalog = self._connection.execute(
                """
                SELECT class_index, label, class_count, observations
                FROM sound_catalog_v2
                WHERE node_mac=? AND boot_id=? AND model_short_sha=? AND location=?
                ORDER BY class_index
                """,
                (
                    row["node_mac"],
                    row["boot_id"],
                    row["model_short_sha"],
                    row["location"],
                ),
            ).fetchall()
            session = self._connection.execute(
                """
                SELECT report_class_count, report_semantic, node_location,
                       node_class_count, node_model, node_semantic, node_config,
                       node_last_reason, metadata_consistent, last_seen_at
                FROM sound_session_v2
                WHERE node_mac=? AND boot_id=? AND model_short_sha=? AND location=?
                """,
                (
                    row["node_mac"],
                    row["boot_id"],
                    row["model_short_sha"],
                    row["location"],
                ),
            ).fetchone()
            result.append(
                {
                    "node_mac": row["node_mac"],
                    "boot_id": row["boot_id"],
                    "location": row["location"],
                    "model": row["model_short_sha"],
                    "class_count": row["class_count"],
                    "catalog": [dict(item) for item in catalog],
                    "session_metadata": dict(session) if session is not None else {},
                    "last_inference": self._row_to_inference(row),
                }
            )
        return result

    def inference_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM sound_inference_v2"
        ).fetchone()
        return int(row["count"])

    def diagnostic_count(self, kind: str | None = None) -> int:
        if kind is None:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM sound_diagnostic_v2"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM sound_diagnostic_v2 WHERE kind=?",
                (kind,),
            ).fetchone()
        return int(row["count"])

    def _catalog_diagnostics(
        self,
        inference: SoundInference,
    ) -> list[SoundDiagnostic]:
        rows = self._connection.execute(
            """
            SELECT class_index, label, class_count
            FROM sound_catalog_v2
            WHERE node_mac=? AND boot_id=? AND model_short_sha=? AND location=?
            """,
            (
                inference.node_mac,
                inference.boot_id,
                inference.model_short_sha,
                inference.location,
            ),
        ).fetchall()
        diagnostics: list[SoundDiagnostic] = []
        observed_counts = {int(row["class_count"]) for row in rows}
        if observed_counts and inference.class_count not in observed_counts:
            diagnostics.append(
                SoundDiagnostic(
                    "sound_catalog_mismatch",
                    "class_count_changed:"
                    f"observed={sorted(observed_counts)},received={inference.class_count}",
                )
            )
        for row in rows:
            if (
                int(row["class_index"]) == inference.class_index
                and str(row["label"]) != inference.label
            ):
                diagnostics.append(
                    SoundDiagnostic(
                        "sound_catalog_mismatch",
                        "index_label_changed:"
                        f"index={inference.class_index},"
                        f"observed={row['label']},received={inference.label}",
                    )
                )
        return diagnostics

    def _upsert_catalog(self, inference: SoundInference) -> None:
        self._connection.execute(
            """
            INSERT INTO sound_catalog_v2 (
                node_mac, boot_id, model_short_sha, location, class_index,
                label, class_count, first_seen_at, last_seen_at, observations
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(
                node_mac, boot_id, model_short_sha, location, class_index
            ) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                observations=sound_catalog_v2.observations + 1
            """,
            (
                inference.node_mac,
                inference.boot_id,
                inference.model_short_sha,
                inference.location,
                inference.class_index,
                inference.label,
                inference.class_count,
                inference.received_at,
                inference.received_at,
            ),
        )

    def _upsert_session(
        self,
        inference: SoundInference,
        node: NodeSoundMetadata | None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO sound_session_v2 (
                node_mac, boot_id, model_short_sha, location,
                report_class_count, report_semantic, node_location,
                node_class_count, node_model, node_semantic, node_config,
                node_last_reason, metadata_consistent, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                node_mac, boot_id, model_short_sha, location
            ) DO UPDATE SET
                report_class_count=excluded.report_class_count,
                report_semantic=excluded.report_semantic,
                node_location=excluded.node_location,
                node_class_count=excluded.node_class_count,
                node_model=excluded.node_model,
                node_semantic=excluded.node_semantic,
                node_config=excluded.node_config,
                node_last_reason=excluded.node_last_reason,
                metadata_consistent=excluded.metadata_consistent,
                last_seen_at=excluded.last_seen_at
            """,
            (
                inference.node_mac,
                inference.boot_id,
                inference.model_short_sha,
                inference.location,
                inference.class_count,
                inference.semantic,
                node.location if node is not None else None,
                node.class_count if node is not None else None,
                node.model if node is not None else None,
                node.semantic if node is not None else None,
                node.config if node is not None else None,
                node.last_reason if node is not None else None,
                int(inference.metadata_consistent),
                inference.received_at,
            ),
        )

    def _write_diagnostics(
        self,
        received_at: float,
        mac: str,
        raw: str,
        diagnostics: tuple[SoundDiagnostic, ...],
        *,
        in_transaction: bool = False,
    ) -> None:
        if not diagnostics:
            return

        def write() -> None:
            self._connection.executemany(
                """
                INSERT INTO sound_diagnostic_v2 (
                    received_at, node_mac, kind, reason, raw_payload
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        received_at,
                        normalize_mac(mac),
                        diagnostic.kind,
                        diagnostic.reason,
                        raw,
                    )
                    for diagnostic in diagnostics
                ],
            )

        if in_transaction:
            write()
        else:
            with self._connection:
                write()

    @staticmethod
    def _row_to_inference(row: sqlite3.Row) -> dict[str, object]:
        return {
            "received_at": datetime.fromtimestamp(
                float(row["received_at"]), timezone.utc
            ).isoformat(),
            "node_timestamp_ms": row["node_timestamp_ms"],
            "duration_ms": row["duration_ms"],
            "location": row["location"],
            "class_index": row["class_index"],
            "class_count": row["class_count"],
            "label": row["label"],
            "semantic": row["semantic"],
            "confidence": row["confidence"],
            "model": row["model_short_sha"],
            "source": row["source"],
            "rms": row["rms"],
            "estimated_db_spl": row["estimated_db_spl"],
            "adl_eligible": bool(row["adl_eligible"]),
            "metadata_consistent": bool(row["metadata_consistent"]),
        }


def parse_sound_inference(event: ReportEvent) -> SoundInference:
    fields = event.packet.fields
    if fields.get("src", "").strip().upper() != "SOUND":
        raise SoundInferenceValidationError("src must be SOUND")
    if fields.get("event", "").strip().upper() != "INFERENCE":
        raise SoundInferenceValidationError("event must be INFERENCE")
    if event.packet.duplicate_fields:
        duplicated = sorted(
            set(event.packet.duplicate_fields).intersection(REQUIRED_FIELDS)
        )
        if duplicated:
            raise SoundInferenceValidationError(
                f"duplicate required field: {duplicated[0]}"
            )
    missing = [key for key in REQUIRED_FIELDS if not fields.get(key, "")]
    if missing:
        raise SoundInferenceValidationError(
            f"missing required field: {missing[0]}"
        )
    if fields["schema"] != "2":
        raise SoundInferenceValidationError("schema must be 2")

    boot_id = fields["bid"].lower()
    if not HEX8.fullmatch(boot_id):
        raise SoundInferenceValidationError("bid must be exactly 8 hexadecimal characters")
    location = fields["location"]
    if location not in LOCATIONS:
        raise SoundInferenceValidationError(
            "location must be ENTRY, LIVING, BEDROOM, KITCHEN or TOILET"
        )
    class_count = _integer(fields, "class_count")
    if not 2 <= class_count <= 20:
        raise SoundInferenceValidationError("class_count must be in [2,20]")
    class_index = _integer(fields, "class_index")
    if not 0 <= class_index < class_count:
        raise SoundInferenceValidationError(
            "class_index must be non-negative and less than class_count"
        )
    confidence = _number(fields, "confidence")
    if not 0.0 <= confidence <= 1.0:
        raise SoundInferenceValidationError("confidence must be in [0,1]")
    model = fields["model"].lower()
    if not HEX8.fullmatch(model):
        raise SoundInferenceValidationError(
            "model must be exactly 8 hexadecimal characters"
        )
    source = fields["source"]
    if source not in SOURCES:
        raise SoundInferenceValidationError("source must be tflm or rms_gate")
    timestamp_ms = _integer(fields, "ts")
    duration_ms = _integer(fields, "duration_ms")
    if not 0 <= timestamp_ms <= UINT32_MAX:
        raise SoundInferenceValidationError("ts must be uint32")
    if not 0 <= duration_ms <= UINT32_MAX:
        raise SoundInferenceValidationError("duration_ms must be uint32")

    return SoundInference(
        node_mac=normalize_mac(event.mac),
        boot_id=boot_id,
        received_at=event.receipt_timestamp or event.timestamp,
        node_timestamp_ms=timestamp_ms,
        duration_ms=duration_ms,
        location=location,
        class_index=class_index,
        class_count=class_count,
        label=fields["label"],
        semantic=fields["semantic"],
        confidence=confidence,
        model_short_sha=model,
        source=source,
        rms=_optional_number(fields, "rms"),
        estimated_db_spl=_optional_number(fields, "db"),
        adl_eligible=fields["semantic"].strip().lower() != "unknown",
        metadata_consistent=True,
    )


def _protocol_diagnostics(inference: SoundInference) -> list[SoundDiagnostic]:
    diagnostics: list[SoundDiagnostic] = []
    is_background = inference.label == "background"
    if inference.class_index == 0 and not is_background:
        diagnostics.append(
            SoundDiagnostic(
                "sound_protocol_mismatch",
                "class_index=0 requires label=background",
            )
        )
    if inference.class_index != 0 and is_background:
        diagnostics.append(
            SoundDiagnostic(
                "sound_protocol_mismatch",
                "label=background requires class_index=0",
            )
        )
    if inference.source == "rms_gate" and (
        inference.class_index != 0 or not is_background
    ):
        diagnostics.append(
            SoundDiagnostic(
                "sound_protocol_mismatch",
                "source=rms_gate requires class_index=0,label=background",
            )
        )
    return diagnostics


def _metadata_diagnostics(
    inference: SoundInference,
    node: NodeSoundMetadata | None,
) -> list[SoundDiagnostic]:
    if node is None:
        return []
    diagnostics: list[SoundDiagnostic] = []
    semantic_state = str(node.semantic or "").strip().lower()
    if semantic_state in {"0", "false", "disabled"}:
        diagnostics.append(
            SoundDiagnostic(
                "sound_metadata_mismatch",
                "cached NODE/CONFIG semantic state is disabled",
            )
        )
    config_state = str(node.config or "").strip().upper()
    if config_state and config_state != "READY":
        diagnostics.append(
            SoundDiagnostic(
                "sound_metadata_mismatch",
                f"cached config state is {config_state}",
            )
        )
    if node.last_reason and config_state == "REJECTED":
        diagnostics.append(
            SoundDiagnostic(
                "sound_metadata_mismatch",
                f"latest CONFIG/REJECTED reason={node.last_reason}",
            )
        )
    comparisons = (
        (
            "location",
            str(node.location or "").strip().upper() or None,
            inference.location,
        ),
        ("class_count", node.class_count, inference.class_count),
        (
            "model",
            str(node.model or "").lower()[:8] or None,
            inference.model_short_sha,
        ),
    )
    for name, cached, received in comparisons:
        if cached is not None and cached != received:
            diagnostics.append(
                SoundDiagnostic(
                    "sound_metadata_mismatch",
                    f"{name}:cached={cached},received={received}",
                )
            )
    return diagnostics


def _integer(fields: dict[str, str], key: str) -> int:
    try:
        return int(fields[key], 10)
    except ValueError as exc:
        raise SoundInferenceValidationError(f"{key} must be an integer") from exc


def _number(fields: dict[str, str], key: str) -> float:
    try:
        value = float(fields[key])
    except ValueError as exc:
        raise SoundInferenceValidationError(f"{key} must be numeric") from exc
    if value != value or value in {float("inf"), float("-inf")}:
        raise SoundInferenceValidationError(f"{key} must be finite")
    return value


def _optional_number(fields: dict[str, str], key: str) -> float | None:
    if key not in fields or fields[key] == "":
        return None
    return _number(fields, key)
