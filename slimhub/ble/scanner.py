from __future__ import annotations

import asyncio

from bleak import BleakScanner


def create_scanner(detection_callback) -> BleakScanner:
    """One long-running scanner; every advertisement goes to the callback."""
    return BleakScanner(detection_callback=detection_callback)


async def power_cycle_adapter(adapter: str = "hci0", off_seconds: float = 2.0) -> None:
    """Turn the BlueZ adapter off and on through D-Bus (no sudo needed).

    A stuck discovery session (StartDiscovery/StopDiscovery returning
    org.bluez.Error.InProgress) survives a daemon restart but not this.
    """
    from dbus_fast import BusType, Variant
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        path = f"/org/bluez/{adapter}"
        proxy = bus.get_proxy_object("org.bluez", path, await bus.introspect("org.bluez", path))
        properties = proxy.get_interface("org.freedesktop.DBus.Properties")
        await properties.call_set("org.bluez.Adapter1", "Powered", Variant("b", False))
        await asyncio.sleep(off_seconds)
        await properties.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
    finally:
        bus.disconnect()
