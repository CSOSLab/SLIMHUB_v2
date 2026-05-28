from __future__ import annotations

from bleak import BleakScanner


async def discover_named_devices(name: str, timeout: float) -> list[object]:
    devices = await BleakScanner.discover(return_adv=True, timeout=timeout)
    matches: list[object] = []
    for device, advertisement in devices.values():
        device_name = getattr(device, "name", None)
        local_name = getattr(advertisement, "local_name", None)
        if device_name == name or local_name == name:
            matches.append(device)
    return matches
