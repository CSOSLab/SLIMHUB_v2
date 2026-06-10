from __future__ import annotations

from datetime import datetime
from pathlib import Path

from slimhub.config import DEFAULT_LOCATION, AppPaths
from slimhub.events import CommandEvent


class UnitspaceMovementLogger:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    async def log(self, timestamp: float, commands: list[CommandEvent]) -> None:
        line = self._line_for(timestamp, commands)
        if line is None:
            return
        path = self._path_for(timestamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")

    def _path_for(self, timestamp: float) -> Path:
        date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        return self.paths.logs_dir / "unitspace" / f"{date}.log"

    def _line_for(self, timestamp: float, commands: list[CommandEvent]) -> str | None:
        if not commands:
            return None

        timestamp_text = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        enter = self._first_command(commands, "enter")
        exit_ = self._first_command(commands, "exit")

        if enter is not None and exit_ is not None:
            message = (
                f"{self._location(exit_)} : EXIT >>>> "
                f"{self._location(enter)} : ENTER"
            )
        elif enter is not None:
            message = f"{self._location(enter)} : ENTER"
        elif exit_ is not None:
            message = f"{self._location(exit_)} : EXIT"
        else:
            return None

        return f"{timestamp_text}    INFO ~~~ {message}"

    def _first_command(
        self,
        commands: list[CommandEvent],
        command: str,
    ) -> CommandEvent | None:
        for item in commands:
            if item.command == command:
                return item
        return None

    def _location(self, command: CommandEvent) -> str:
        return command.location or DEFAULT_LOCATION
