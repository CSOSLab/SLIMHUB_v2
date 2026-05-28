from __future__ import annotations

import json


def encode_request(command: str, args: dict[str, object] | None = None) -> bytes:
    return (
        json.dumps({"command": command, "args": args or {}}, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def decode_response(line: bytes) -> dict[str, object]:
    if not line:
        raise RuntimeError("daemon returned an empty response")
    response = json.loads(line.decode("utf-8"))
    if not isinstance(response, dict):
        raise RuntimeError("daemon response must be a JSON object")
    return response
