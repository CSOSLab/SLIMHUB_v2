"""DEAN Node validation LOGDATA: framing, decoding, durable store and ACK."""
from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from slimhub.config import AppPaths
from slimhub.logging.validation_store import ValidationStore
from slimhub.protocol.nus import (
    FrameAssembler,
    LogDataPacket,
    build_frame,
    parse_frame,
    validate_command_payload,
)
from slimhub.protocol.validation_log import ack_target, build_validation_ack_command

MAC = "90:E5:B1:D1:23:36"
# Node reference ENV record (dean_node_v2 tools/slimhub_validation.py GOLDEN_ENV_HEX).
GOLDEN_ENV = bytes.fromhex(
    "444c010100001c00010000000000000039300000000000000b00000007000000"
    "1900c40900001efb64000000900100000100f9151f1f1f02030000008a7bb22e"
)


def vle1(stream=7, bundle=1, text=b"ENTER"):
    return b"VLE1" + struct.pack("<QIBIQB", stream, bundle, 1, 9, 1000, len(text)) + text


def vls1(state=1, close=0):
    return b"VLS1" + struct.pack("<QBBBBBBBBHQ", 7, state, close, 1, 0, 0, 0, 7, 0, 300, 9)


def vlg1(stream=7, bundle=1):
    return b"VLG1" + struct.pack("<QI", stream, bundle) + GOLDEN_ENV


class LogDataProtocolTest(unittest.TestCase):
    def test_assembler_accepts_logdata_between_reports_in_small_chunks(self):
        stream = (
            build_frame(MAC, "REPORT", b"src=EVENT,event=BASELINE")
            + build_frame(MAC, "LOGDATA", vls1())
            + build_frame(MAC, "LOGDATA", vle1())
            + build_frame(MAC, "REPORT", b"src=SOUND,event=TIME_REQUEST")
        )
        assembler = FrameAssembler()
        frames = []
        for i in range(0, len(stream), 20):
            frames.extend(assembler.push(stream[i : i + 20]))
        types = [parse_frame(f).packet_type for f in frames]
        self.assertEqual(types, ["REPORT", "LOGDATA", "LOGDATA", "REPORT"])

    def test_decode_and_ack_targets(self):
        event = parse_frame(build_frame(MAC, "LOGDATA", vle1())).parsed
        self.assertIsInstance(event, LogDataPacket)
        self.assertEqual((event.format, event.value["text"]), ("VLE1", "ENTER"))
        self.assertEqual(ack_target(event.value), ("event", 1, 1))
        detail = parse_frame(build_frame(MAC, "LOGDATA", vlg1())).parsed
        self.assertEqual(detail.value["records"][0]["seq"], 1)
        self.assertEqual(ack_target(detail.value), ("log", 1, 1))
        status = parse_frame(build_frame(MAC, "LOGDATA", vls1(3, 6))).parsed
        self.assertEqual((status.value["state"], status.value["close_reason"]),
                         ("aborted", "connect_timeout"))
        self.assertIsNone(ack_target(status.value))

    def test_invalid_logdata_is_returned_with_error(self):
        bad = parse_frame(build_frame(MAC, "LOGDATA", b"VLE1" + b"\0" * 10)).parsed
        self.assertIsNotNone(bad.error)
        self.assertIsNone(bad.value)

    def test_validation_ack_command(self):
        text = build_validation_ack_command("log", 7, 1, 42)
        self.assertEqual(validate_command_payload(text), text)
        self.assertEqual(text, "validation_ack,kind=log,stream=0000000000000007,bundle=1,through=42")
        with self.assertRaises(ValueError):
            build_validation_ack_command("battery", 7, 1, 3)
        with self.assertRaises(ValueError):
            validate_command_payload("validation_ack,kind=log,stream=7,bundle=1,through=1")


class ValidationStoreTest(unittest.TestCase):
    def test_durable_append_dedupe_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ValidationStore(AppPaths(Path(tmp)))
            event = parse_frame(build_frame(MAC, "LOGDATA", vle1())).parsed
            status = parse_frame(build_frame(MAC, "LOGDATA", vls1())).parsed
            self.assertTrue(store.store(MAC, event, vle1(), 1.0))
            self.assertFalse(store.store(MAC, event, vle1(), 2.0))
            self.assertTrue(store.store(MAC, status, vls1(), 3.0))
            self.assertTrue(store.store(MAC, status, vls1(), 4.0))
            path = store.path_for(MAC, 7)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r["format"] for r in rows], ["VLE1", "VLS1", "VLS1"])
            self.assertEqual(rows[0]["decoded"]["text"], "ENTER")
            reloaded = ValidationStore(AppPaths(Path(tmp)))
            self.assertFalse(reloaded.store(MAC, event, vle1(), 5.0))


class DaemonLogDataTest(unittest.TestCase):
    def _daemon(self, store):
        from slimhub.daemon import SlimHubDaemon

        sent = []

        async def send_command(command):
            sent.append(command)
            return True

        async def log_structured(event):
            sent.append(("structured", event.kind))

        fake = SimpleNamespace(
            logger=SimpleNamespace(info=lambda *a: None, debug=lambda *a: None,
                                   warning=lambda *a: None, error=lambda *a: None),
            raw_logger=SimpleNamespace(log_structured=log_structured),
            validation_store=store,
            config_store=SimpleNamespace(load=lambda mac: SimpleNamespace(location="kitchen")),
            registry=SimpleNamespace(send_command=send_command),
        )
        handler = SlimHubDaemon._handle_logdata.__get__(fake)
        return handler, sent

    def test_store_then_ack_and_no_ack_for_status_or_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, sent = self._daemon(ValidationStore(AppPaths(Path(tmp))))
            asyncio.run(handler(MAC, parse_frame(build_frame(MAC, "LOGDATA", vle1()))))
            asyncio.run(handler(MAC, parse_frame(build_frame(MAC, "LOGDATA", vls1()))))
            asyncio.run(handler(MAC, parse_frame(build_frame(MAC, "LOGDATA", b"VLE1" + b"\0" * 10))))
            commands = [c.command for c in sent if hasattr(c, "command")]
            self.assertEqual(commands,
                             ["validation_ack,kind=event,stream=0000000000000007,bundle=1,through=1"])
            self.assertIn(("structured", "validation_status"), sent)
            self.assertIn(("structured", "validation_logdata_rejected"), sent)

    def test_no_ack_when_store_fails(self):
        class FailingStore:
            def store(self, *args):
                raise OSError("disk full")

        handler, sent = self._daemon(FailingStore())
        asyncio.run(handler(MAC, parse_frame(build_frame(MAC, "LOGDATA", vle1()))))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
