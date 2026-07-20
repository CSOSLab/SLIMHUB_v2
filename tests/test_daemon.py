from __future__ import annotations

import json
import struct
import tempfile
import sys
import types
import unittest
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.events import CommandEvent
from slimhub.protocol.nus import (
    ParsedFrame,
    RawDataPacket,
    ReportPacket,
    build_frame,
    mac_to_bytes,
    parse_frame,
)


class FakeBleakClient:
    pass


class FakeBleakScanner:
    @staticmethod
    async def discover(*args: object, **kwargs: object) -> dict[object, object]:
        return {}


class FakeBleakError(Exception):
    pass


bleak_module = sys.modules.get("bleak") or types.ModuleType("bleak")
bleak_module.BleakClient = FakeBleakClient
bleak_module.BleakScanner = FakeBleakScanner
sys.modules["bleak"] = bleak_module

bleak_exc_module = sys.modules.get("bleak.exc") or types.ModuleType("bleak.exc")
bleak_exc_module.BleakError = FakeBleakError
sys.modules["bleak.exc"] = bleak_exc_module

from slimhub.daemon import SlimHubDaemon


class FakeSession:
    def __init__(self, address: str, sink: list[tuple[str, str]] | None = None) -> None:
        self.address = address
        self.name = "DEAN_NODE_V2"
        self.commands: list[CommandEvent] = []
        self.sink = sink

    async def send_command(self, command: CommandEvent) -> None:
        self.commands.append(command)
        if self.sink is not None:
            self.sink.append((command.command, command.address))

    def status(self) -> dict[str, object]:
        return {"address": self.address, "connected": True}

    async def stop(self) -> None:
        return None


def raw_packet(detected: int) -> RawDataPacket:
    return RawDataPacket(
        flag_human_presence=1 if detected else 0,
        detected=detected,
        flag_env=0,
        temperature_c=0.0,
        humidity=0,
        iaq=0,
        eco2=0,
        bvoc=0,
        accuracy=0,
        flag_sound=0,
        sound=[0] * 16,
        is_pir_human_detection_event=detected == 1,
    )


def raw_frame(address: str, detected: int) -> ParsedFrame:
    payload = bytes([1 if detected else 0, detected]) + (b"\x00" * 31)
    return ParsedFrame(
        mac=address,
        mac_bytes=mac_to_bytes(address),
        packet_type="RAWDATA",
        packet_length=len(payload),
        payload=payload,
        parsed=raw_packet(detected),
    )


def report_frame(address: str, message: str, fields: dict[str, str]) -> ParsedFrame:
    payload = message.encode("utf-8")
    return ParsedFrame(
        mac=address,
        mac_bytes=mac_to_bytes(address),
        packet_type="REPORT",
        packet_length=len(payload),
        payload=payload,
        parsed=ReportPacket(message=message, fields=fields),
    )


def json_report_frame(address: str, document: dict[str, object]) -> ParsedFrame:
    return parse_frame(
        build_frame(address, "REPORT", json.dumps(document).encode("utf-8"))
    )


def inout_report_frame(address: str, action: str = "ENTER") -> ParsedFrame:
    is_enter = action.upper() == "ENTER"
    message = (
        f"src=INOUT,event={action.upper()},signal={'enter' if is_enter else 'exit'},"
        f"code={10 if is_enter else 20},pir=1,radar={1 if is_enter else 0},"
        f"dist_cm={75 if is_enter else 0},"
        f"state={'inside_moving' if is_enter else 'outside'},reason=test"
    )
    return report_frame(
        address,
        message,
        {
            "src": "INOUT",
            "event": action.upper(),
            "signal": "enter" if is_enter else "exit",
            "code": "10" if is_enter else "20",
            "pir": "1",
            "radar": "1" if is_enter else "0",
            "dist_cm": "75" if is_enter else "0",
            "state": "inside_moving" if is_enter else "outside",
            "reason": "test",
        },
    )


def usd_report_frame(address: str) -> ParsedFrame:
    fields = {
        "src": "USD",
        "event": "STATUS",
        "uptime": "12345",
        "file": "LOG/001.CSV",
        "ok": "1",
        "batt_valid": "1",
        "batt_v": "3.980",
        "batt_mv": "3980",
        "batt_pct": "75",
        "batt_rem_mah": "1125",
        "batt_cap_mah": "1500",
        "usb": "0",
        "chg": "1",
        "sd": "0",
        "pir": "0",
        "radar": "0",
        "human": "0",
        "temp_c": "27.10",
        "hum_pct": "52.30",
    }
    message = ",".join(f"{key}={value}" for key, value in fields.items())
    return report_frame(address, message, fields)


def sound_report_frame(address: str, event: str, **extra: str) -> ParsedFrame:
    fields = {
        "src": "SOUND",
        "event": event,
        "cid": "00ab12cd",
        **extra,
    }
    return report_frame(
        address,
        ",".join(f"{key}={value}" for key, value in fields.items()),
        fields,
    )


def audio_frame(address: str) -> ParsedFrame:
    pcm = struct.pack("<512h", *range(-256, 256))
    payload = (
        struct.pack(
            "<BBBBIIIHH",
            1,
            1,
            1,
            1,
            0x00AB12CD,
            0,
            0,
            512,
            len(pcm),
        )
        + pcm
    )
    return parse_frame(build_frame(address, "AUDIO", payload))


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    def test_json_device_requires_canonical_uppercase_colon_mac(self) -> None:
        address = "AA:BB:CC:DD:EE:01"
        frame = json_report_frame(
            address,
            {"device": "aa:bb:cc:dd:ee:01", "type": "EVENT"},
        )

        self.assertEqual(
            SlimHubDaemon._json_identity_warning(frame),
            "noncanonical_device:aa:bb:cc:dd:ee:01",
        )

    async def test_json_event_validates_device_identity_and_never_feeds_estimator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            frame_mac = "AA:BB:CC:DD:EE:01"
            document = {
                "device": "11:22:33:44:55:66",
                "type": "EVENT",
                "event": "ENTER",
                "value": 10,
                "schema": 2,
                "bid": "12ab34cd",
                "eid": 41,
                "ts": 840000,
            }
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            daemon.config_store.set_field(frame_mac, "location", "ENTRY")

            with self.assertLogs("slimhub.daemon", level="WARNING") as captured:
                await daemon.handle_frame(frame_mac, json_report_frame(frame_mac, document))
                await daemon.flush_report_reorder_buffer()

            self.assertIn("device_mismatch:11:22:33:44:55:66", "\n".join(captured.output))
            self.assertIsNone(daemon.estimator.snapshot()["last_address"])
            timeline = daemon.multimodal.snapshot()["legacy_events"]
            self.assertIn(f"{frame_mac}/12ab34cd/840000/ENTER", timeline)
            report_file = next((Path(tmpdir) / "programdata" / "reports").glob("*.jsonl"))
            rows = [
                json.loads(line)
                for line in report_file.read_text(encoding="utf-8").splitlines()
            ]
            raw = next(row for row in rows if row["kind"] == "report")
            self.assertEqual(raw["mac"], frame_mac)
            self.assertEqual(raw["report_format"], "json")
            self.assertEqual(raw["json_document"]["device"], "11:22:33:44:55:66")
            self.assertIn("device_mismatch", raw["identity_warning"])

    async def test_invalid_json_enter_value_is_preserved_but_not_timeline_movement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            document = {
                "device": address,
                "type": "EVENT",
                "event": "ENTER",
                "value": 20,
                "schema": 2,
                "bid": "12ab34cd",
                "eid": 41,
                "ts": 840000,
                "future_key": "preserved",
            }
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))

            await daemon.handle_frame(address, json_report_frame(address, document))
            await daemon.flush_report_reorder_buffer()

            self.assertEqual(daemon.multimodal.snapshot()["legacy_events"], {})
            report_file = next((Path(tmpdir) / "programdata" / "reports").glob("*.jsonl"))
            rows = [
                json.loads(line)
                for line in report_file.read_text(encoding="utf-8").splitlines()
            ]
            invalid = next(row for row in rows if row["kind"] == "legacy_event_invalid")
            self.assertEqual(invalid["raw_document"]["future_key"], "preserved")

    async def test_new_node_enter_sends_exit_command_to_previous_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            entry = FakeSession("AA:BB:CC:DD:EE:01")
            living = FakeSession("AA:BB:CC:DD:EE:02")
            await daemon.registry.add(entry)
            await daemon.registry.add(living)
            daemon.config_store.set_field(entry.address, "location", "ENTRY")
            daemon.config_store.set_field(living.address, "location", "LIVING")

            await daemon.handle_frame(entry.address, raw_frame(entry.address, detected=10))
            await daemon.handle_frame(living.address, raw_frame(living.address, detected=10))

            self.assertEqual([command.command for command in entry.commands], ["enter", "exit"])
            self.assertEqual([command.location for command in entry.commands], ["ENTRY", "ENTRY"])
            self.assertEqual([command.command for command in living.commands], ["enter"])
            self.assertEqual(living.commands[0].location, "LIVING")

    async def test_inout_report_updates_power_shadow_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))

            await daemon.handle_frame(address, inout_report_frame(address))

            snapshot = daemon.power_shadow.snapshot(address)
            self.assertEqual(snapshot["state"], "RADAR_CONFIRMED_ACTIVE")
            self.assertEqual(snapshot["last_inout_event"], "ENTER")
            self.assertEqual(snapshot["last_inout_state"], "inside_moving")
            self.assertEqual(snapshot["last_inout_code"], "10")

    async def test_inout_report_enter_sends_enter_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            daemon.config_store.set_field(address, "location", "ENTRY")

            await daemon.handle_frame(address, inout_report_frame(address, "ENTER"))

            self.assertEqual([command.command for command in session.commands], ["enter"])
            self.assertEqual(session.commands[0].location, "ENTRY")

    async def test_report_command_routes_by_frame_mac_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ble_address = "AA:BB:CC:DD:EE:01"
            frame_mac = "11:22:33:44:55:66"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(ble_address)
            await daemon.registry.add(session)

            await daemon.handle_frame(ble_address, inout_report_frame(frame_mac, "ENTER"))

            self.assertEqual([command.command for command in session.commands], ["enter"])
            self.assertEqual(session.commands[0].address, frame_mac)
            self.assertEqual(session.commands[0].canonical_node_id, frame_mac)
            self.assertEqual(session.commands[0].ble_address, ble_address)

    async def test_report_enter_new_node_sends_enter_new_then_exit_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sent: list[tuple[str, str]] = []
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            entry = FakeSession("AA:BB:CC:DD:EE:01", sent)
            living = FakeSession("AA:BB:CC:DD:EE:02", sent)
            await daemon.registry.add(entry)
            await daemon.registry.add(living)
            daemon.config_store.set_field(entry.address, "location", "ENTRY")
            daemon.config_store.set_field(living.address, "location", "LIVING")

            await daemon.handle_frame(entry.address, inout_report_frame(entry.address, "ENTER"))
            await daemon.handle_frame(living.address, inout_report_frame(living.address, "ENTER"))

            self.assertEqual(
                sent,
                [
                    ("enter", "AA:BB:CC:DD:EE:01"),
                    ("enter", "AA:BB:CC:DD:EE:02"),
                    ("exit", "AA:BB:CC:DD:EE:01"),
                ],
            )

    async def test_inout_report_exit_sends_exit_and_clears_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)

            await daemon.handle_frame(address, inout_report_frame(address, "ENTER"))
            await daemon.handle_frame(address, inout_report_frame(address, "EXIT"))

            self.assertEqual([command.command for command in session.commands], ["enter", "exit"])
            self.assertIsNone(daemon.estimator.snapshot()["last_address"])

    async def test_malformed_inout_report_does_not_raise_or_send_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            frame = report_frame(
                address,
                "src=INOUT,event=BOGUS,signal=?,code=oops",
                {
                    "src": "INOUT",
                    "event": "BOGUS",
                    "signal": "?",
                    "code": "oops",
                },
            )

            await daemon.handle_frame(address, frame)

            self.assertEqual(session.commands, [])

    async def test_usd_status_is_available_without_routine_audit_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            daemon.config_store.set_field(address, "location", "ENTRY")

            await daemon.handle_frame(address, usd_report_frame(address))

            self.assertFalse((Path(tmpdir) / "programdata" / "reports").exists())

            response = await daemon.dispatch(
                {"command": "battery.status", "args": {"address": address}}
            )
            status = response["data"]
            self.assertEqual(status["address"], address)
            self.assertEqual(status["location"], "ENTRY")
            self.assertEqual(status["batt_mv"], 3980)
            self.assertEqual(status["batt_v"], 3.98)
            self.assertEqual(status["batt_pct"], 75)
            self.assertEqual(status["batt_rem_mah"], 1125)
            self.assertEqual(status["usb"], 0)
            self.assertEqual(status["chg"], 1)
            self.assertEqual(status["sd"], 0)
            self.assertEqual(status["file"], "LOG/001.CSV")
            self.assertEqual(status["uptime"], 12345)
            self.assertEqual(status["ok"], 1)

    async def test_explicit_sound_capture_stores_audio_without_estimator_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)

            await daemon.send_command(
                address,
                "sound_start,label=pee,dest=ble,thr=800,max=60,silence=5",
            )
            await daemon.handle_frame(
                address,
                sound_report_frame(
                    address,
                    "CAPTURE_ARMED",
                    label="pee",
                    dest="ble",
                ),
            )
            await daemon.handle_frame(
                address,
                sound_report_frame(
                    address,
                    "CAPTURE_START",
                    label="pee",
                    dest="ble",
                ),
            )
            await daemon.handle_frame(address, audio_frame(address))
            await daemon.handle_frame(
                address,
                sound_report_frame(
                    address,
                    "CAPTURE_DONE",
                    samples="512",
                    blocks="1",
                    queue_drop="0",
                    ble_drop="0",
                    reason="command_stop",
                ),
            )

            root = Path(tmpdir) / "data" / "sound" / address / "pee"
            manifest = json.loads(
                (root / "00ab12cd.json").read_text(encoding="utf-8")
            )
            self.assertTrue((root / "00ab12cd.wav").exists())
            self.assertTrue(manifest["complete"])
            self.assertIsNone(daemon.estimator.snapshot()["last_address"])
            self.assertEqual(session.commands[0].command.split(",")[0], "sound_start")

    async def test_two_node_ack_replay_confirms_only_destination_without_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            a = FakeSession("AA:BB:CC:DD:EE:01")
            b = FakeSession("AA:BB:CC:DD:EE:02")
            await daemon.registry.add(a)
            await daemon.registry.add(b)
            daemon.config_store.set_field(a.address, "location", "ENTRY")
            daemon.config_store.set_field(b.address, "location", "LIVING")

            await daemon.handle_frame(a.address, raw_frame(a.address, 10))
            await daemon.handle_frame(
                a.address,
                report_frame(
                    a.address,
                    "src=INOUT,event=SEQUENCE,result=ENTER_CONFIRMED,event_id=D0,boot_id=a,event_seq=1,event_ts_ms=1000",
                    {
                        "src": "INOUT", "event": "SEQUENCE", "result": "ENTER_CONFIRMED",
                        "event_id": "D0", "boot_id": "a", "event_seq": "1", "event_ts_ms": "1000",
                    },
                ),
            )
            await daemon.handle_frame(b.address, raw_frame(b.address, 10))
            await daemon.handle_frame(
                b.address,
                report_frame(
                    b.address,
                    "src=INOUT,event=SEQUENCE,result=ENTER_CONFIRMED,event_id=D0,boot_id=b,event_seq=1,event_ts_ms=2000",
                    {
                        "src": "INOUT", "event": "SEQUENCE", "result": "ENTER_CONFIRMED",
                        "event_id": "D0", "boot_id": "b", "event_seq": "1", "event_ts_ms": "2000",
                    },
                ),
            )
            await daemon.handle_frame(
                a.address,
                report_frame(
                    a.address,
                    "src=INOUT,event=SEQUENCE,result=EXIT_CONFIRMED,event_id=D1,boot_id=a,event_seq=2,event_ts_ms=2200",
                    {
                        "src": "INOUT", "event": "SEQUENCE", "result": "EXIT_CONFIRMED",
                        "event_id": "D1", "boot_id": "a", "event_seq": "2", "event_ts_ms": "2200",
                    },
                ),
            )

            await daemon.flush_report_reorder_buffer()

            self.assertEqual([command.command for command in a.commands], ["enter", "exit"])
            self.assertEqual([command.command for command in b.commands], ["enter"])
            self.assertEqual(
                daemon.estimator.snapshot()["confirmed_occupants"],
                [b.address],
            )

    async def test_malformed_inout_event_and_sequence_do_not_stop_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)

            for message, fields in (
                (
                    "src=INOUT,event=EVENT,id=C0,primary_seq=bad",
                    {"src": "INOUT", "event": "EVENT", "id": "C0", "primary_seq": "bad"},
                ),
                (
                    "src=INOUT,event=SEQUENCE,result=ENTER_CONFIRMED,event_id=D0,event_seq=bad",
                    {"src": "INOUT", "event": "SEQUENCE", "result": "ENTER_CONFIRMED", "event_id": "D0", "event_seq": "bad"},
                ),
            ):
                await daemon.handle_frame(address, report_frame(address, message, fields))

            self.assertFalse(daemon.stop_event.is_set())
            self.assertEqual(session.commands, [])

    async def test_multimodal_env_report_is_typed_logged_and_not_estimator_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            fields = {
                "src": "EVENT", "event": "ENV", "schema": "1", "boot_id": "boot-a",
                "session_seq": "7", "analysis_seq": "51", "event_id": "E1",
                "event_ts_ms": "1100", "confidence": "73", "baseline": "51.00",
                "peak": "58.00", "delta_levels": "2", "start_ms": "1000", "duration_ms": "100",
            }
            message = ",".join(f"{key}={value}" for key, value in fields.items())

            await daemon.handle_frame(address, report_frame(address, message, fields))
            await daemon.flush_report_reorder_buffer()

            self.assertEqual(session.commands, [])
            response = await daemon.dispatch({"command": "multimodal.status", "args": {}})
            session_state = response["data"]["sessions"][f"{address}/boot-a/7"]
            self.assertEqual(session_state["records"][0]["canonical_name"], "humidity")
            self.assertFalse((Path(tmpdir) / "programdata" / "reports").exists())


if __name__ == "__main__":
    unittest.main()
