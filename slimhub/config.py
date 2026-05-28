from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from slimhub.protocol.nus import normalize_mac


DEFAULT_LOCATION = "undefined"


@dataclass(frozen=True)
class AppPaths:
    base_dir: Path

    @classmethod
    def from_base(cls, base_dir: str | os.PathLike[str] | None = None) -> "AppPaths":
        if base_dir is None:
            base_dir = os.environ.get("SLIMHUB_HOME") or Path.cwd()
        return cls(Path(base_dir).resolve())

    @property
    def programdata_dir(self) -> Path:
        return self.base_dir / "programdata"

    @property
    def config_dir(self) -> Path:
        return self.programdata_dir / "config"

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def socket_path(self) -> Path:
        return self.programdata_dir / "slimhub.sock"

    def ensure(self) -> None:
        self.programdata_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class DeviceConfig:
    address: str
    name: str = ""
    location: str = DEFAULT_LOCATION

    def __post_init__(self) -> None:
        self.address = normalize_mac(self.address)
        self.location = self.location or DEFAULT_LOCATION


class DeviceConfigStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.paths.ensure()

    def _path_for(self, address: str) -> Path:
        return self.paths.config_dir / f"{normalize_mac(address)}.json"

    def load(self, address: str) -> DeviceConfig:
        normalized = normalize_mac(address)
        path = self._path_for(normalized)
        if not path.exists():
            return DeviceConfig(address=normalized)

        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return DeviceConfig(
            address=data.get("address", normalized),
            name=data.get("name", ""),
            location=data.get("location", DEFAULT_LOCATION),
        )

    def save(self, config: DeviceConfig) -> DeviceConfig:
        config.address = normalize_mac(config.address)
        config.location = config.location or DEFAULT_LOCATION
        path = self._path_for(config.address)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(asdict(config), f, indent=2, ensure_ascii=False)
            f.write("\n")
        return config

    def set_field(self, address: str, field: str, value: str) -> DeviceConfig:
        if field not in {"name", "location"}:
            raise ValueError("field must be 'name' or 'location'")
        config = self.load(address)
        setattr(config, field, value or (DEFAULT_LOCATION if field == "location" else ""))
        return self.save(config)

    def list_all(self) -> list[DeviceConfig]:
        configs: list[DeviceConfig] = []
        for path in sorted(self.paths.config_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            configs.append(
                DeviceConfig(
                    address=data.get("address", path.stem),
                    name=data.get("name", ""),
                    location=data.get("location", DEFAULT_LOCATION),
                )
            )
        return configs
