from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from slimhub.config import AppPaths, HubConfigStore


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DatabaseConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MySQLSettings:
    host: str
    port: int
    user: str
    password: str
    database: str


class ReportDatabaseUpdater:
    """Cron-friendly JSONL → local MySQL → remote MySQL incremental pipeline.

    It deliberately consumes v2's structured JSONL rather than the legacy CSV
    files. Credentials are read only from environment variables; no database
    secret is kept in the repository or cron template.
    """

    def __init__(self, paths: AppPaths, environ: dict[str, str] | None = None) -> None:
        self.paths = paths
        self.environ = dict(os.environ if environ is None else environ)
        self.adl_table = self.environ.get(
            "SLIMHUB_DB_ADL_TABLE",
            self.environ.get("ADL_TABLE", "event_adl"),
        )
        self.inout_table = self.environ.get("SLIMHUB_DB_INOUT_TABLE", "in_out")
        self.house_mac = (
            self.environ.get("SLIMHUB_HOUSE_MAC")
            or HubConfigStore(paths).load_or_create().address
        )
        _validate_identifier(self.adl_table)
        _validate_identifier(self.inout_table)

    def update(self, *, upload: bool = True) -> dict[str, object]:
        started_at = _now()
        try:
            result = {"ingest": self.ingest()}
            result["upload"] = self.upload() if upload else {"skipped": True}
        except Exception as exc:
            self._write_run_status(
                {
                    "command": "update",
                    "started_at": started_at,
                    "finished_at": _now(),
                    "ok": False,
                    "error": str(exc),
                }
            )
            raise
        self._write_run_status(
            {
                "command": "update",
                "started_at": started_at,
                "finished_at": _now(),
                "ok": True,
                **result,
            }
        )
        return result

    def status(self) -> dict[str, object]:
        """Return safe local evidence for cron and remote-upload verification."""
        return {
            "local_database": self._database_status("LOCAL"),
            "remote_database": self._database_status("REMOTE"),
            "ingest_offsets": self._read_json(self.paths.db_ingest_offset_path),
            "upload_offsets": self._read_json(self.paths.db_upload_offset_path),
            "last_ingest": self._read_json(self.paths.db_ingest_status_path) or None,
            "last_upload": self._read_json(self.paths.db_upload_status_path) or None,
            "last_update": self._read_json(self.paths.db_status_path) or None,
        }

    def ingest(self) -> dict[str, object]:
        started_at = _now()
        try:
            result = self._ingest()
        except Exception as exc:
            self._write_operation_status(
                self.paths.db_ingest_status_path,
                command="ingest",
                started_at=started_at,
                error=exc,
            )
            raise
        self._write_operation_status(
            self.paths.db_ingest_status_path,
            command="ingest",
            started_at=started_at,
            result=result,
        )
        return result

    def _ingest(self) -> dict[str, object]:
        records, new_offsets = self._read_new_records()
        adl_rows, inout_rows = self.rows_from_records(
            records,
            house_mac=self.house_mac,
        )
        if adl_rows or inout_rows:
            local = self._settings("LOCAL", required=True)
            with self._connect(local) as connection:
                with connection.cursor() as cursor:
                    if adl_rows:
                        cursor.executemany(
                            f"INSERT INTO `{self.adl_table}` "
                            "(house_mac, location, created_time, event_sequence, adl, truth_value) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            adl_rows,
                        )
                    if inout_rows:
                        cursor.executemany(
                            f"INSERT INTO `{self.inout_table}` "
                            "(house_mac, location, created_time, direction) "
                            "VALUES (%s, %s, %s, %s)",
                            inout_rows,
                        )
                connection.commit()
        self._write_json_atomic(self.paths.db_ingest_offset_path, new_offsets)
        return {
            "records": len(records),
            "adl_inserted": len(adl_rows),
            "inout_inserted": len(inout_rows),
        }

    def upload(self) -> dict[str, object]:
        started_at = _now()
        try:
            result = self._upload()
        except Exception as exc:
            self._write_operation_status(
                self.paths.db_upload_status_path,
                command="upload",
                started_at=started_at,
                error=exc,
            )
            raise
        self._write_operation_status(
            self.paths.db_upload_status_path,
            command="upload",
            started_at=started_at,
            result=result,
        )
        return result

    def _upload(self) -> dict[str, object]:
        remote = self._settings("REMOTE", required=False)
        if remote is None:
            return {"skipped": True, "reason": "SLIMHUB_REMOTE_DB_HOST is not configured"}
        local = self._settings("LOCAL", required=True)
        offsets = self._read_json(self.paths.db_upload_offset_path)
        result: dict[str, object] = {}
        with self._connect(local) as local_connection, self._connect(remote) as remote_connection:
            for stream, table in (("ADL", self.adl_table), ("INOUT", self.inout_table)):
                last_id = _as_int(offsets.get(stream), 0)
                columns = self._columns_without_id(local_connection, table)
                if not columns:
                    result[stream.lower()] = {"uploaded": 0, "last_id": last_id}
                    continue
                column_list = ", ".join(f"`{column}`" for column in columns)
                with local_connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT id, {column_list} FROM `{table}` "
                        "WHERE id > %s ORDER BY id ASC LIMIT %s",
                        (
                            last_id,
                            _as_int(
                                self.environ.get("SLIMHUB_DB_UPLOAD_BATCH_SIZE")
                                or self.environ.get("UPLOAD_BATCH_SIZE"),
                                1000,
                            ),
                        ),
                    )
                    rows = cursor.fetchall()
                if not rows:
                    result[stream.lower()] = {"uploaded": 0, "last_id": last_id}
                    continue
                placeholders = ", ".join(["%s"] * len(columns))
                with remote_connection.cursor() as cursor:
                    cursor.executemany(
                        f"INSERT INTO `{table}` ({column_list}) VALUES ({placeholders})",
                        [row[1:] for row in rows],
                    )
                remote_connection.commit()
                offsets[stream] = rows[-1][0]
                # Persist each stream independently. If the following table
                # fails, a committed stream must not be uploaded twice.
                self._write_json_atomic(self.paths.db_upload_offset_path, offsets)
                result[stream.lower()] = {"uploaded": len(rows), "last_id": rows[-1][0]}
        self._write_json_atomic(self.paths.db_upload_offset_path, offsets)
        return result

    @staticmethod
    def rows_from_records(
        records: list[dict[str, object]],
        *,
        house_mac: str,
    ) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
        adl_rows: list[tuple[object, ...]] = []
        inout_rows: list[tuple[object, ...]] = []
        for record in records:
            kind = str(record.get("kind") or "")
            mac = str(record.get("mac") or "")
            location = str(record.get("location") or "undefined")
            created_time = _created_time(record)
            if not mac or created_time is None:
                continue
            display_location = f"{location}:{mac}"
            if kind == "raw":
                parsed = record.get("parsed")
                if not isinstance(parsed, dict) or _as_int(parsed.get("flag_human_presence"), 0) != 1:
                    continue
                inout_rows.append(
                    (
                        house_mac,
                        display_location,
                        created_time,
                        _as_int(parsed.get("detected"), 0),
                    )
                )
            elif kind == "adl_result" and _as_bool(record.get("final")):
                adl_rows.append(
                    (
                        house_mac,
                        mac,
                        created_time,
                        str(record.get("sequence") or ""),
                        str(record.get("adl") or record.get("event") or "NO_MATCH"),
                        _truth_value(record.get("truth")),
                    )
                )
        return adl_rows, inout_rows

    def _read_new_records(self) -> tuple[list[dict[str, object]], dict[str, int]]:
        offsets = {key: _as_int(value, 0) for key, value in self._read_json(self.paths.db_ingest_offset_path).items()}
        records: list[dict[str, object]] = []
        updated = dict(offsets)
        today = datetime.now().strftime("%Y-%m-%d")
        backfill = _as_bool(self.environ.get("SLIMHUB_DB_BACKFILL"))
        for path in sorted((self.paths.programdata_dir / "reports").glob("*.jsonl")):
            key = str(path.resolve())
            offset = offsets.get(key, 0)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if key not in offsets and not backfill and path.stem != today:
                # The deployed v1 cron only discovered today's files. Preserve
                # that first-run behavior unless an explicit backfill is requested.
                updated[key] = size
                continue
            if offset > size:
                offset = 0
            end_offset = offset
            with path.open("rb") as source:
                source.seek(offset)
                while True:
                    line_start = source.tell()
                    raw_line = source.readline()
                    if not raw_line:
                        break
                    if not raw_line.endswith(b"\n"):
                        # The daemon may still be appending this JSONL line.
                        source.seek(line_start)
                        break
                    end_offset = source.tell()
                    try:
                        decoded = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(decoded, dict):
                        records.append(decoded)
            updated[key] = end_offset
        return records, updated

    def _settings(self, name: str, *, required: bool) -> MySQLSettings | None:
        prefix = f"SLIMHUB_{name}_DB_"
        prefixes = [prefix]
        if name == "LOCAL":
            prefixes.extend(("LOCAL_DB_", "ADL_DB_"))
        else:
            prefixes.append("REMOTE_DB_")

        def configured_value(*suffixes: str) -> str | None:
            return next(
                (
                    value
                    for candidate in prefixes
                    for suffix in suffixes
                    if (value := self.environ.get(candidate + suffix))
                ),
                None,
            )

        host = configured_value("HOST")
        if not host:
            if required:
                raise DatabaseConfigurationError(f"{prefix}HOST must be configured")
            return None
        user = configured_value("USER") or ""
        database = configured_value("NAME") or ""
        if required and not user:
            raise DatabaseConfigurationError(f"{prefix}USER must be configured")
        if required and not database:
            raise DatabaseConfigurationError(f"{prefix}NAME must be configured")
        return MySQLSettings(
            host=host,
            port=_as_int(configured_value("PORT"), 3306),
            user=user,
            password=configured_value("PASS", "PASSWORD") or "",
            database=database,
        )

    def _database_status(self, name: str) -> dict[str, object]:
        settings = self._settings(name, required=False)
        if settings is None:
            return {"configured": False}
        return {
            "configured": bool(settings.user and settings.database),
            "host": settings.host,
            "port": settings.port,
            "database": settings.database or None,
        }

    @staticmethod
    def _connect(settings: MySQLSettings):
        try:
            import pymysql
        except ModuleNotFoundError as exc:
            raise DatabaseConfigurationError(
                "PyMySQL is required for db commands; install project dependencies first"
            ) from exc
        return pymysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            database=settings.database,
            charset="utf8mb4",
            autocommit=False,
        )

    @staticmethod
    def _columns_without_id(connection: Any, table: str) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute(f"SHOW COLUMNS FROM `{table}`")
            return [str(row[0]) for row in cursor.fetchall() if str(row[0]).lower() != "id"]

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _write_run_status(self, value: dict[str, object]) -> None:
        self._write_json_atomic(self.paths.db_status_path, value)

    def _write_operation_status(
        self,
        path: Path,
        *,
        command: str,
        started_at: str,
        result: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        value: dict[str, object] = {
            "command": command,
            "started_at": started_at,
            "finished_at": _now(),
            "ok": error is None,
        }
        if result is not None:
            value["result"] = result
        if error is not None:
            value["error"] = str(error)
        self._write_json_atomic(path, value)


def _validate_identifier(value: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise DatabaseConfigurationError(f"invalid SQL table identifier: {value!r}")


def _as_int(value: object, default: int | None = None) -> int:
    try:
        return int(str(value), 10)
    except (TypeError, ValueError):
        if default is None:
            raise
        return default


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _truth_value(value: object) -> float | None:
    try:
        truth = float(str(value))
    except (TypeError, ValueError):
        return None
    return truth / 100 if truth > 1 else truth


def _created_time(record: dict[str, object]) -> str | None:
    raw_time = record.get("receipt_ts")
    if raw_time is None:
        raw_time = record.get("timestamp")
    try:
        return datetime.fromtimestamp(float(raw_time)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        value = record.get("time")
        return str(value).replace("T", " ").split(".")[0] if value else None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
