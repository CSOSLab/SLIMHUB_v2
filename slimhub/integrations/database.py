from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from slimhub.config import AppPaths, HubConfigStore
from slimhub.integrations.data_ingest import DataDirectoryReader


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


class DataDirectoryDatabaseUpdater:
    """Cron-friendly data/ → local MySQL → remote MySQL pipeline.

    Collection and DB synchronization are deliberately separate: the daemon
    writes legacy-compatible rawdata CSV and debugstr JSONL files, while this
    class incrementally consumes those files. Credentials are read only from
    environment variables.
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
        reader = DataDirectoryReader(
            self.paths,
            offsets=self._read_json(self.paths.db_ingest_offset_path),
            environ=self.environ,
        )
        batch = reader.read(house_mac=self.house_mac)
        adl_rows = batch.adl_rows
        inout_rows = batch.inout_rows
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
        self._write_json_atomic(self.paths.db_ingest_offset_path, batch.offsets)
        return {
            "files": batch.files,
            "lines": batch.lines,
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


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Compatibility for callers that imported the earlier JSONL-oriented name.
ReportDatabaseUpdater = DataDirectoryDatabaseUpdater
