from __future__ import annotations

import asyncio

from slimhub.config import AppPaths
from slimhub.cli.server import decode_response, encode_request


async def send_request(
    paths: AppPaths,
    command: str,
    args: dict[str, object] | None = None,
) -> object:
    reader, writer = await asyncio.open_unix_connection(str(paths.socket_path))
    try:
        writer.write(encode_request(command, args))
        await writer.drain()
        response = decode_response(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()

    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "daemon request failed"))
    return response.get("data")


def send_request_sync(
    paths: AppPaths,
    command: str,
    args: dict[str, object] | None = None,
) -> object:
    return asyncio.run(send_request(paths, command, args))


def daemon_is_running(paths: AppPaths) -> bool:
    """Probe the existing socket instead of trusting a possibly stale path."""
    if not paths.socket_path.exists():
        return False
    try:
        asyncio.run(
            asyncio.wait_for(
                send_request(paths, "devices"),
                timeout=0.5,
            )
        )
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return False
    return True
