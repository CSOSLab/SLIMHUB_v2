from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from slimhub.cli.client import send_request
from slimhub.cli.server import decode_response, encode_request
from slimhub.config import AppPaths


class CliServerTests(unittest.IsolatedAsyncioTestCase):
    def test_json_line_helpers(self) -> None:
        request = encode_request("devices", {"x": 1})
        self.assertTrue(request.endswith(b"\n"))
        response = decode_response(b'{"ok": true, "data": [], "error": null}\n')
        self.assertTrue(response["ok"])

    async def test_client_reads_fake_daemon_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            paths.ensure()

            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                request = json.loads((await reader.readline()).decode("utf-8"))
                writer.write(
                    (
                        json.dumps(
                            {
                                "ok": True,
                                "data": {"command": request["command"]},
                                "error": None,
                            }
                        )
                        + "\n"
                    ).encode("utf-8")
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=str(paths.socket_path))
            try:
                data = await send_request(paths, "devices")
            finally:
                server.close()
                await server.wait_closed()
                Path(paths.socket_path).unlink(missing_ok=True)

            self.assertEqual(data, {"command": "devices"})


if __name__ == "__main__":
    unittest.main()
