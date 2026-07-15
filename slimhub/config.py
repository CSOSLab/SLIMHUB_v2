from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from slimhub.protocol.nus import DEFAULT_DEVICE_NAME, normalize_mac


DEFAULT_LOCATION = "undefined"
DEFAULT_DEVICE_TYPE = DEFAULT_DEVICE_NAME


def get_mac_address() -> str:
    mac = uuid.getnode()
    return ":".join(f"{(mac >> shift) & 0xFF:02X}" for shift in range(40, -1, -8))


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

    @property
    def hub_config_path(self) -> Path:
        return self.programdata_dir / "config.json"

    @property
    def logging_path(self) -> Path:
        return self.programdata_dir / "logging.log"

    @property
    def deployment_manifest_path(self) -> Path:
        return self.programdata_dir / "deployment_manifest.json"

    @property
    def display_path(self) -> Path:
        """Current operator-facing display feed (append-only text)."""
        return self.programdata_dir / "display.txt"

    @property
    def display_dir(self) -> Path:
        """Daily display archives, compatible with the legacy data/display layout."""
        return self.data_dir / "display"

    @property
    def db_sync_dir(self) -> Path:
        return self.programdata_dir / "db_sync"

    @property
    def db_ingest_offset_path(self) -> Path:
        return self.db_sync_dir / "data_offsets.json"

    @property
    def db_upload_offset_path(self) -> Path:
        return self.db_sync_dir / "upload_offsets.json"

    @property
    def db_status_path(self) -> Path:
        """Outcome of the most recent combined database update."""
        return self.db_sync_dir / "last_update.json"

    @property
    def db_ingest_status_path(self) -> Path:
        return self.db_sync_dir / "last_ingest.json"

    @property
    def db_upload_status_path(self) -> Path:
        return self.db_sync_dir / "last_upload.json"

    def ensure(self) -> None:
        self.programdata_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_sync_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class DeviceConfig:
    address: str
    type: str = DEFAULT_DEVICE_TYPE
    name: str = ""
    location: str = DEFAULT_LOCATION

    def __post_init__(self) -> None:
        self.address = normalize_mac(self.address)
        self.type = self.type or DEFAULT_DEVICE_TYPE
        self.location = self.location or DEFAULT_LOCATION


@dataclass
class HubConfig:
    address: str
    type: str = "slimhub"
    owner: str | None = None
    name: str | None = None


class HubConfigStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.paths.ensure()

    def load_or_create(self) -> HubConfig:
        if not self.paths.hub_config_path.exists():
            return self.save(HubConfig(address=get_mac_address()))

        with self.paths.hub_config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return HubConfig(
            address=data.get("address") or get_mac_address(),
            type=data.get("type") or "slimhub",
            owner=data.get("owner"),
            name=data.get("name"),
        )

    def save(self, config: HubConfig) -> HubConfig:
        self.paths.hub_config_path.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.hub_config_path.open("w", encoding="utf-8") as f:
            json.dump(asdict(config), f, indent=4, ensure_ascii=False)
            f.write("\n")
        return config

    def set_field(self, field: str, value: str) -> HubConfig:
        if field not in {"address", "type", "owner", "name"}:
            raise ValueError("hub config field must be one of: address, type, owner, name")
        config = self.load_or_create()
        setattr(config, field, value)
        return self.save(config)


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
            type=data.get("type", DEFAULT_DEVICE_TYPE),
            name=data.get("name", ""),
            location=data.get("location", DEFAULT_LOCATION),
        )

    def save(self, config: DeviceConfig) -> DeviceConfig:
        config.address = normalize_mac(config.address)
        config.type = config.type or DEFAULT_DEVICE_TYPE
        config.location = config.location or DEFAULT_LOCATION
        path = self._path_for(config.address)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(asdict(config), f, indent=4, ensure_ascii=False)
            f.write("\n")
        return config

    def set_field(self, address: str, field: str, value: str) -> DeviceConfig:
        if field not in {"type", "name", "location"}:
            raise ValueError("field must be 'type', 'name' or 'location'")
        config = self.load(address)
        if field == "location":
            value = value or DEFAULT_LOCATION
        elif field == "type":
            value = value or DEFAULT_DEVICE_TYPE
        setattr(config, field, value)
        return self.save(config)

    def ensure(
        self,
        address: str,
        *,
        device_type: str = DEFAULT_DEVICE_TYPE,
        name: str = "",
    ) -> DeviceConfig:
        existed = self._path_for(address).exists()
        config = self.load(address)
        if (not existed or not config.type) and device_type:
            config.type = device_type
        if (not existed or not config.name) and name:
            config.name = name
        return self.save(config)

    def list_all(self) -> list[DeviceConfig]:
        configs: list[DeviceConfig] = []
        for path in sorted(self.paths.config_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            configs.append(
                DeviceConfig(
                    address=data.get("address", path.stem),
                    type=data.get("type", DEFAULT_DEVICE_TYPE),
                    name=data.get("name", ""),
                    location=data.get("location", DEFAULT_LOCATION),
                )
            )
        return configs
