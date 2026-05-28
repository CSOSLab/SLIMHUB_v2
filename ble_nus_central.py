#!/usr/bin/env python3
"""Compatibility BLE NUS Central reader.

The production daemon lives behind the `slimhub run` CLI. This script keeps the
original single-device packet reader available for quick hardware checks.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Sequence
from contextlib import suppress

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from slimhub.protocol.nus import (
    DEFAULT_DEVICE_NAME,
    NUS_TX_NOTIFY_UUID,
    FrameAssembler,
    PacketParseError,
    describe_frame,
    hex_dump,
    parse_frame,
)


async def find_device_by_name_or_address(
    *,
    name: str,
    address: str | None,
    timeout: float,
    logger: logging.Logger,
) -> object | str | None:
    if address:
        logger.info("Scanning for BLE address %s for %.1f seconds", address, timeout)
        device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if device is not None:
            logger.info("Found device by address: %s (%s)", device.name, device.address)
            return device
        logger.warning("Address %s was not seen; trying direct connection fallback", address)
        return address

    logger.info("Scanning for BLE name %r for %.1f seconds", name, timeout)

    def match_name(device: object, advertisement_data: object) -> bool:
        return (
            getattr(device, "name", None) == name
            or getattr(advertisement_data, "local_name", None) == name
        )

    device = await BleakScanner.find_device_by_filter(match_name, timeout=timeout)
    if device is None:
        logger.warning("Could not find device named %r", name)
        return None
    logger.info("Found device: %s (%s)", device.name, device.address)
    return device


def build_notify_handler(
    assembler: FrameAssembler,
    logger: logging.Logger,
):
    def handle_notify(sender: object, data: bytearray) -> None:
        chunk = bytes(data)
        logger.info("notify sender=%s chunk_len=%d hex=%s", sender, len(chunk), hex_dump(chunk))
        for frame_bytes in assembler.push(chunk):
            try:
                frame = parse_frame(frame_bytes)
            except PacketParseError as exc:
                logger.error("parse_error=%s frame_hex=%s", exc, hex_dump(frame_bytes))
                continue
            logger.info("parsed %s", describe_frame(frame))

    return handle_notify


async def wait_or_stop(stop_event: asyncio.Event, delay_seconds: float) -> None:
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)


async def run(args: argparse.Namespace) -> None:
    logger = logging.getLogger("ble_nus_central")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        logger.info("Shutdown requested")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_shutdown)

    while not stop_event.is_set():
        device = await find_device_by_name_or_address(
            name=args.name,
            address=args.address,
            timeout=args.scan_timeout,
            logger=logger,
        )
        if device is None:
            await wait_or_stop(stop_event, args.reconnect_delay)
            continue

        disconnected_event = asyncio.Event()

        def on_disconnect(_: BleakClient) -> None:
            logger.warning("Disconnected")
            loop.call_soon_threadsafe(disconnected_event.set)

        try:
            logger.info("Connecting to %s", device)
            async with BleakClient(device, disconnected_callback=on_disconnect) as client:
                logger.info("Connected: %s", client.is_connected)
                assembler = FrameAssembler()
                await client.start_notify(
                    NUS_TX_NOTIFY_UUID,
                    build_notify_handler(assembler, logger),
                )
                done, pending = await asyncio.wait(
                    {
                        asyncio.create_task(stop_event.wait()),
                        asyncio.create_task(disconnected_event.wait()),
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    with suppress(asyncio.CancelledError):
                        task.result()
                with suppress(BleakError, RuntimeError):
                    await client.stop_notify(NUS_TX_NOTIFY_UUID)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("BLE session failed")

        if not stop_event.is_set():
            await wait_or_stop(stop_event, args.reconnect_delay)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Single-device BLE NUS packet reader for DEAN_NODE_V2."
    )
    parser.add_argument("--name", default=DEFAULT_DEVICE_NAME)
    parser.add_argument("--address")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--scan-timeout", type=float, default=10.0)
    parser.add_argument("--reconnect-delay", type=float, default=3.0)
    return parser.parse_args(argv)


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    args = parse_args()
    setup_logging(args.debug)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
