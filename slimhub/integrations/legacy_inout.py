"""Derive the v1-style (one active room per house) IN/OUT file from rawdata.

DEAN Nodes now decide IN/OUT per room, so several rooms can be IN at once.
The remote DB procedures were built on the v1 hub output, where the hub kept
one house-wide active room (one resident, one active Node):

* first Node ENTER (rawdata Direction 10) in room B -> ``B 10``
* ENTER in the active room -> nothing
* ENTER in room B while room A is active -> ``A 20`` then ``B 10``, same time
* a Node's own EXIT (Direction 20) is ignored and there is no timeout, so the
  last room stays IN until another room ENTERs. (v1 Nodes did not send their
  own EXIT; ignoring it keeps the v1 result for today's standalone Nodes.)

Output: ``data/legacy_inout/YYYY-MM-DD.txt``, one display-style line per row,
which ``DataDirectoryReader`` loads into ``in_out`` from the cut-over date.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta

from slimhub.config import AppPaths
from slimhub.integrations.data_ingest import (
    LEGACY_INOUT_DIRNAME,
    _data_path_identity,
    _read_rawdata,
    legacy_inout_from,
    legacy_inout_line,
    legacy_inout_state_path,
)


# Same-second tie-break after the active room (whose ENTER is a no-op).
ROOM_ORDER = ("ENTRY", "LIVING", "BEDROOM", "KITCHEN", "TOILET")
DEFAULT_LAG_SECONDS = 60


class LegacyInOutError(RuntimeError):
    pass


class LegacyInOutGenerator:
    def __init__(
        self,
        paths: AppPaths,
        *,
        from_date: str,
        lag_seconds: int = DEFAULT_LAG_SECONDS,
    ) -> None:
        self.paths = paths
        self.from_date = from_date
        self.lag_seconds = lag_seconds
        self.output_dir = paths.data_dir / LEGACY_INOUT_DIRNAME
        self.state_path = legacy_inout_state_path(paths)

    @classmethod
    def from_environ(
        cls,
        paths: AppPaths,
        from_date: str,
        environ: dict[str, str] | None = None,
    ) -> "LegacyInOutGenerator":
        environ = dict(os.environ if environ is None else environ)
        try:
            lag = int(environ.get("SLIMHUB_LEGACY_INOUT_LAG_SECONDS", DEFAULT_LAG_SECONDS))
        except ValueError:
            lag = DEFAULT_LAG_SECONDS
        return cls(paths, from_date=from_date, lag_seconds=max(lag, 0))

    def run(
        self,
        *,
        now: datetime | None = None,
        rebuild: bool = False,
        dry_run: bool = False,
    ) -> dict[str, object]:
        """Append rows for rawdata ENTERs older than now - lag.

        Without a state file (first run) or with rebuild, existing output files
        from the cut-over date are moved to a backup folder and regenerated.
        dry_run computes from a fresh state and writes nothing.
        """
        state = None if rebuild or dry_run else self._load_state()
        if state is not None and state.get("from_date") != self.from_date:
            raise LegacyInOutError(
                f"state from_date {state.get('from_date')} != {self.from_date}; "
                "run scripts/legacy_inout.py --rebuild"
            )
        if state is None:
            state = {"from_date": self.from_date, "active_room": None,
                     "active_mac": None, "last_time": None, "offsets": {}, "outputs": {}}
            if not dry_run:
                self._move_outputs_to_backup()
        elif not dry_run:
            self._truncate_uncommitted(state["outputs"])

        watermark = ((now or datetime.now()) - timedelta(seconds=self.lag_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        events, offsets = self._read_enters(state["offsets"], watermark)
        late = sum(1 for created, _, _ in events
                   if state["last_time"] is not None and created < state["last_time"])
        rows = self._apply_rules(events, state)

        per_day: dict[str, int] = {}
        for created, *_ in rows:
            per_day[created[:10]] = per_day.get(created[:10], 0) + 1
        result: dict[str, object] = {
            "from_date": self.from_date,
            "watermark": watermark,
            "enters": len(events),
            "late_enters": late,
            "rows": len(rows),
            "per_day": per_day,
            "active_room": state["active_room"],
        }
        if dry_run:
            result["sample"] = [legacy_inout_line(*row).rstrip("\n") for row in rows[:10]]
            return result

        self._append(rows, state["outputs"])
        state["offsets"] = offsets
        self._save_state(state)
        return result

    # ------------------------------------------------------------------

    def _read_enters(
        self,
        offsets: dict[str, int],
        watermark: str,
    ) -> tuple[list[tuple[str, str, str]], dict[str, int]]:
        """Return (time, room, mac) Node ENTERs in time order and new offsets."""
        updated = dict(offsets)
        events: list[tuple[str, str, str]] = []
        paths = sorted(self.paths.data_dir.glob("*/*/*/inference/rawdata/*.txt"))
        for path in paths:
            identity = _data_path_identity(self.paths.data_dir, path)
            if identity is None or path.stem < self.from_date:
                continue
            room, mac, _stream = identity
            key = str(path.resolve())
            start = int(updated.get(key, 0))
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if start > size:
                start = 0
            if start == size:
                continue
            _adl, rows, end_offset, _consumed = _read_rawdata(
                path, start, house_mac="", location=room, path_mac=mac, until=watermark
            )
            updated[key] = end_offset
            events.extend(
                (str(created), room, mac)
                for _house, _location, created, direction in rows
                if direction == 10 and str(created) >= self.from_date
            )
        # sort is stable: same-time rows keep file order before the tie-break.
        events.sort(key=lambda event: event[0])
        return events, updated

    @staticmethod
    def _apply_rules(
        events: list[tuple[str, str, str]],
        state: dict[str, object],
    ) -> list[tuple[str, str, str, int]]:
        rows: list[tuple[str, str, str, int]] = []
        index = 0
        while index < len(events):
            created = events[index][0]
            group_end = index
            while group_end < len(events) and events[group_end][0] == created:
                group_end += 1
            active_at_start = state["active_room"]
            group = sorted(
                events[index:group_end],
                key=lambda event: (event[1] != active_at_start,
                                   ROOM_ORDER.index(event[1]), event[2]),
            )
            for _time, room, mac in group:
                if room == state["active_room"]:
                    continue
                if state["active_room"] is not None:
                    rows.append((created, state["active_room"], state["active_mac"], 20))
                rows.append((created, room, mac, 10))
                state["active_room"] = room
                state["active_mac"] = mac
            if state["last_time"] is None or created > state["last_time"]:
                state["last_time"] = created
            index = group_end
        return rows

    def _append(self, rows: list[tuple[str, str, str, int]], outputs: dict[str, int]) -> None:
        by_day: dict[str, list[str]] = {}
        for row in rows:
            by_day.setdefault(row[0][:10], []).append(legacy_inout_line(*row))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for day, lines in sorted(by_day.items()):
            path = self.output_dir / f"{day}.txt"
            with path.open("a", encoding="utf-8") as output:
                output.writelines(lines)
                output.flush()
                os.fsync(output.fileno())
            outputs[day] = path.stat().st_size

    def _truncate_uncommitted(self, outputs: dict[str, int]) -> None:
        """Cut lines written after the last saved state (crash before save)."""
        for day, size in outputs.items():
            path = self.output_dir / f"{day}.txt"
            try:
                if path.stat().st_size > int(size):
                    with path.open("r+b") as output:
                        output.truncate(int(size))
            except FileNotFoundError:
                continue

    def _move_outputs_to_backup(self) -> None:
        existing = [path for path in sorted(self.output_dir.glob("*.txt"))
                    if path.stem >= self.from_date]
        if not existing:
            return
        backup = self.paths.db_sync_dir / (
            "legacy_inout_backup_" + datetime.now().strftime("%Y%m%d%H%M%S")
        )
        backup.mkdir(parents=True, exist_ok=True)
        for path in existing:
            shutil.move(str(path), backup / path.name)
        # The DB reader must start the regenerated files from byte 0.
        offsets_path = self.paths.db_ingest_offset_path
        try:
            offsets = json.loads(offsets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        moved = {str(path.resolve()) for path in existing} | {str(path) for path in existing}
        if isinstance(offsets, dict) and moved & offsets.keys():
            kept = {key: value for key, value in offsets.items() if key not in moved}
            temporary = offsets_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(kept, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(offsets_path)

    def _load_state(self) -> dict[str, object] | None:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return state if isinstance(state, dict) else None

    def _save_state(self, state: dict[str, object]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)


def run_from_environ(
    paths: AppPaths,
    environ: dict[str, str] | None = None,
    **kwargs: object,
) -> dict[str, object] | None:
    """Run the generator when a cut-over date is configured, else return None."""
    environ = dict(os.environ if environ is None else environ)
    from_date = legacy_inout_from(paths, environ)
    if from_date is None:
        return None
    return LegacyInOutGenerator.from_environ(paths, from_date, environ).run(**kwargs)

