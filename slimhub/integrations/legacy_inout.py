"""v1-style (one active room per house) IN/OUT in debugstr, Node IN/OUT apart.

DEAN Nodes now decide IN/OUT per room, so several rooms can be IN at once.
The DB sync (debugstr ``type=EVENT`` ENTER/EXIT -> ``in_out``) and the remote
DB procedures expect the v1 hub output, where the hub kept one house-wide
active room (one resident, one active Node):

* first Node ENTER (RAWDATA detected=10) in room B -> ``B ENTER``
* ENTER in the active room -> nothing
* ENTER in room B while room A is active -> ``A EXIT`` then ``B ENTER``,
  same time
* a Node's own EXIT is ignored and there is no timeout, so the last room
  stays IN until another room ENTERs. (v1 Nodes did not send their own EXIT;
  ignoring it keeps the v1 result for today's standalone Nodes.)

Each room keeps the Node's own reports in
``<room>/<type>/<mac>/inference/new_algo_inout/YYYY-MM-DD.txt``; the derived
rows are written as ``type=EVENT`` lines into the rooms' ``debugstr`` (and the
display timeline). The daemon applies the rules live (LegacyInOutTracker);
``convert_debugstr`` converts days recorded before the daemon did.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from slimhub.config import DEFAULT_DEVICE_TYPE, AppPaths
from slimhub.integrations.data_ingest import _data_path_identity, _read_rawdata


# Same-second tie-break after the active room (whose ENTER is a no-op).
ROOM_ORDER = ("ENTRY", "LIVING", "BEDROOM", "KITCHEN", "TOILET")
NEW_ALGO_INOUT_DIRNAME = "new_algo_inout"
_DISPLAY_INOUT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  \S+ \[EVENT\] - (ENTER|EXIT) value: \d+$")


@dataclass(frozen=True)
class LegacyRow:
    time: str  # "YYYY-MM-DD HH:MM:SS"
    room: str
    mac: str
    direction: int  # 10 ENTER, 20 EXIT

    @property
    def event(self) -> str:
        return "ENTER" if self.direction == 10 else "EXIT"


def legacy_event_document(mac: str, direction: int) -> dict[str, object]:
    """The debugstr line the DB sync reads (timestamp is added by the writer)."""
    return {
        "device": mac,
        "type": "EVENT",
        "event": "ENTER" if direction == 10 else "EXIT",
        "value": direction,
    }


def legacy_state_path(paths: AppPaths) -> Path:
    return paths.programdata_dir / "legacy_inout_state.json"


def apply_enter(state: dict[str, object], time_text: str, room: str, mac: str) -> list[LegacyRow]:
    """Apply one Node ENTER to the house token; return the rows to write."""
    if room == state.get("active_room"):
        return []
    rows: list[LegacyRow] = []
    if state.get("active_room"):
        rows.append(LegacyRow(time_text, str(state["active_room"]), str(state["active_mac"]), 20))
    rows.append(LegacyRow(time_text, room, mac, 10))
    state["active_room"] = room
    state["active_mac"] = mac
    state["last_time"] = time_text
    return rows


class LegacyInOutTracker:
    """Live house token for the daemon, kept across restarts in a JSON file."""

    def __init__(self, paths: AppPaths) -> None:
        self.path = legacy_state_path(paths)
        self.state: dict[str, object] = {"active_room": None, "active_mac": None, "last_time": None}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            self.state.update({key: loaded.get(key) for key in self.state})

    def enter(self, time_text: str, room: str, mac: str) -> list[LegacyRow]:
        rows = apply_enter(self.state, time_text, room, mac)
        if rows:
            try:
                _write_json_atomic(self.path, self.state)
            except OSError:
                # Keep the in-memory token and still return the rows: losing
                # them would desync debugstr from the token after a disk error.
                logging.exception("legacy IN/OUT state not saved path=%s", self.path)
        return rows


def legacy_rows_from_rawdata(
    paths: AppPaths,
    from_date: str,
    until: str,
) -> tuple[list[LegacyRow], dict[str, object]]:
    """Rebuild the rows from every room's rawdata Node ENTERs since from_date.

    rawdata is used rather than debugstr because the Node's DEBUG lines are
    missing when a link drops mid-window; the RAWDATA ENTER row is not.
    """
    events: list[tuple[str, str, str]] = []
    for path in sorted(paths.data_dir.glob("*/*/*/inference/rawdata/*.txt")):
        identity = _data_path_identity(paths.data_dir, path)
        if identity is None or path.stem < from_date:
            continue
        room, mac, _stream = identity
        _adl, rows, _end, _consumed = _read_rawdata(
            path, 0, house_mac="", location=room, path_mac=mac, until=until
        )
        events.extend(
            (str(created), room, mac)
            for _house, _location, created, direction in rows
            if direction == 10 and str(created) >= from_date
        )
    events.sort(key=lambda event: event[0])
    state: dict[str, object] = {"active_room": None, "active_mac": None, "last_time": None}
    result: list[LegacyRow] = []
    index = 0
    while index < len(events):
        created = events[index][0]
        end = index
        while end < len(events) and events[end][0] == created:
            end += 1
        active = state["active_room"]
        group = sorted(
            events[index:end],
            key=lambda event: (event[1] != active, ROOM_ORDER.index(event[1]), event[2]),
        )
        for _time, room, mac in group:
            result.extend(apply_enter(state, created, room, mac))
        index = end
    return result, state


def convert_debugstr(
    paths: AppPaths,
    from_date: str,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Convert days from from_date: Node IN/OUT out of debugstr, legacy rows in.

    For every room file from from_date the Node's DEBUG ENTER/EXIT lines move to
    new_algo_inout/<date>.txt, the derived EVENT lines are merged into
    debugstr by time, and display/<date>.txt gets the same IN/OUT lines.
    Originals are backed up first, rewritten debugstr offsets are cleared so
    the DB sync reads them again, and the live tracker continues from the
    final active room. Run with the daemon stopped. Refuses a second run.
    """
    until = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    rows, final_state = legacy_rows_from_rawdata(paths, from_date, until)

    targets: dict[Path, list[LegacyRow]] = {}
    for row in rows:
        targets.setdefault(_debugstr_path(paths, row), []).append(row)
    debug_files = set(targets)
    for path in paths.data_dir.glob("*/*/*/inference/debugstr/*.txt"):
        identity = _data_path_identity(paths.data_dir, path)
        if identity is not None and path.stem >= from_date:
            debug_files.add(path)

    plans: dict[Path, str] = {}
    moved_total = 0
    for path in sorted(debug_files):
        existing = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
        kept: list[str] = []
        moved: list[str] = []
        for line in existing:
            document = _json_or_none(line)
            kind = str(document.get("type") or "").upper() if document else ""
            event = str(document.get("event") or "").upper() if document else ""
            if kind == "EVENT" and event in {"ENTER", "EXIT"}:
                raise RuntimeError(f"{path} already has legacy EVENT lines; already converted")
            (moved if kind == "DEBUG" and event in {"ENTER", "EXIT"} else kept).append(line)
        new_algo = path.parents[1] / NEW_ALGO_INOUT_DIRNAME / path.name
        if moved:
            if new_algo.exists():
                raise RuntimeError(f"{new_algo} exists; already converted")
            plans[new_algo] = "".join(moved)
        legacy_lines = [
            (row.time, json.dumps({**legacy_event_document(row.mac, row.direction),
                                   "timestamp": row.time}, ensure_ascii=False) + "\n")
            for row in targets.get(path, [])
        ]
        plans[path] = "".join(_merge_by_time(kept, legacy_lines, _json_time))
        moved_total += len(moved)

    by_day: dict[str, list[LegacyRow]] = {}
    for row in rows:
        by_day.setdefault(row.time[:10], []).append(row)
    display_files = {paths.display_dir / f"{day}.txt" for day in by_day}
    display_files |= {path for path in paths.display_dir.glob("*.txt")
                      if re.fullmatch(r"\d{4}-\d\d-\d\d", path.stem) and path.stem >= from_date}
    for path in sorted(display_files):
        existing = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
        kept = [line for line in existing if not _DISPLAY_INOUT.match(line.rstrip("\n"))]
        legacy_lines = [
            (row.time, f"{row.time}  {row.room} [EVENT] - {row.event} value: {row.direction}\n")
            for row in by_day.get(path.stem, [])
        ]
        plans[path] = "".join(_merge_by_time(kept, legacy_lines, lambda line: line[:19]))

    result: dict[str, object] = {
        "from_date": from_date,
        "legacy_rows": len(rows),
        "node_lines_moved": moved_total,
        "debugstr_files": len(debug_files),
        "display_files": len(display_files),
        "active_room": final_state["active_room"],
    }
    if dry_run:
        return result

    backup = paths.db_sync_dir / ("backup_debugstr_" + datetime.now().strftime("%Y%m%d%H%M%S"))
    for path in sorted(plans):
        if path.exists():
            target = backup / path.relative_to(paths.base_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for old in (paths.data_dir / "legacy_inout", paths.db_sync_dir / "legacy_inout_state.json"):
        if old.exists():
            target = backup / old.relative_to(paths.base_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), target)
    for path, text in plans.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    _clear_offsets(paths, debug_files)
    _write_json_atomic(legacy_state_path(paths), final_state)
    result["backup"] = str(backup)
    return result


def _debugstr_path(paths: AppPaths, row: LegacyRow) -> Path:
    return (paths.data_dir / row.room / DEFAULT_DEVICE_TYPE / row.mac / "inference"
            / "debugstr" / f"{row.time[:10]}.txt")


def _json_or_none(line: str) -> dict[str, object] | None:
    try:
        document = json.loads(line)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def _json_time(line: str) -> str | None:
    document = _json_or_none(line)
    value = str(document.get("timestamp") or "") if document else ""
    return value if re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", value) else None


def _merge_by_time(kept: list[str], inserted: list[tuple[str, str]], time_of) -> list[str]:
    """Insert (time, line) pairs before the first kept line that is later."""
    pending = sorted(inserted, key=lambda item: item[0])
    merged: list[str] = []
    index = 0
    for line in kept:
        line_time = time_of(line)
        while line_time is not None and index < len(pending) and pending[index][0] <= line_time:
            merged.append(pending[index][1])
            index += 1
        merged.append(line)
    merged.extend(line for _time, line in pending[index:])
    return merged


def _clear_offsets(paths: AppPaths, files: set[Path]) -> None:
    offsets_path = paths.db_ingest_offset_path
    try:
        offsets = json.loads(offsets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(offsets, dict):
        return
    keys = {str(path.resolve()) for path in files} | {str(path) for path in files}
    _write_json_atomic(offsets_path, {key: value for key, value in offsets.items() if key not in keys})


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)
