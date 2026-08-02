from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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


def raw_sound_frame(address: str) -> ParsedFrame:
    payload = struct.pack(
        "<BB7HB16b",
        0,
        0,
        *([0] * 7),
        1,
        *([-64] * 10),
        *([0] * 6),
    )
    return parse_frame(build_frame(address, "RAWDATA", payload))


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
    async def test_connection_initializes_time_status_and_config_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)

            await daemon.handle_connection_state(address, True, 100.0)

            self.assertEqual(
                [command.command.split(",", 1)[0] for command in session.commands],
                ["time_sync", "node_status", "config_get"],
            )
            self.assertIn("epoch_ms=100000", session.commands[0].command)

    async def test_typed_candidate_uses_inout_confirm_and_correlated_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            await daemon.handle_frame(
                address,
                report_frame(
                    address,
                    "src=NODE,event=STATUS,bid=a1b2c3d4,authority=slimhub,"
                    "occupancy=OUT,capture=IDLE,config=READY,semantic=1,location=TOILET",
                    {
                        "src": "NODE",
                        "event": "STATUS",
                        "bid": "a1b2c3d4",
                        "authority": "slimhub",
                        "occupancy": "OUT",
                        "capture": "IDLE",
                        "config": "READY",
                        "semantic": "1",
                        "location": "TOILET",
                    },
                ),
            )
            await daemon.handle_frame(address, raw_frame(address, detected=10))
            await daemon.handle_frame(
                address,
                report_frame(
                    address,
                    "src=INOUT,event=ENTER,signal=enter,code=10,"
                    "state=0,boot_id=a1b2c3d4,event_seq=41,event_ts_ms=100",
                    {
                        "src": "INOUT",
                        "event": "ENTER",
                        "signal": "enter",
                        "code": "10",
                        "state": "0",
                        "boot_id": "a1b2c3d4",
                        "event_seq": "41",
                        "event_ts_ms": "100",
                    },
                ),
            )

            self.assertEqual(len(session.commands), 1)
            command = session.commands[0].command
            self.assertTrue(
                command.startswith(
                    "inout_confirm,bid=a1b2c3d4,cid=41,state=in,rid="
                )
            )
            rid = command.rsplit("=", 1)[1]
            await daemon.handle_frame(
                address,
                report_frame(
                    address,
                    "src=INOUT,event=CONFIRM_ACK,schema=2,bid=a1b2c3d4,"
                    f"cid=41,rid={rid},state=in,source=slimhub,"
                    "applied=1,changed=1,reason=applied,legacy=0,ts=101",
                    {
                        "src": "INOUT",
                        "event": "CONFIRM_ACK",
                        "schema": "2",
                        "bid": "a1b2c3d4",
                        "cid": "41",
                        "rid": rid,
                        "state": "in",
                        "source": "slimhub",
                        "applied": "1",
                        "changed": "1",
                        "reason": "applied",
                        "legacy": "0",
                        "ts": "101",
                    },
                ),
            )

            self.assertEqual(
                daemon.dean_contract.snapshot(address)["occupancy"],
                "IN",
            )
            self.assertEqual(
                daemon.dean_contract.home_snapshot()["confirmed_occupant"],
                address,
            )

    async def test_already_applied_ack_does_not_duplicate_legacy_timeline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            paths = AppPaths.from_base(tmpdir)
            daemon = SlimHubDaemon(paths=paths)
            daemon.config_store.set_field(address, "location", "TOILET")
            session = FakeSession(address)
            await daemon.registry.add(session)
            await daemon.handle_frame(
                address,
                report_frame(
                    address,
                    "src=INOUT,event=ENTER,signal=enter,code=10,state=0,"
                    "boot_id=a1b2c3d4,event_seq=41,event_ts_ms=100",
                    {
                        "src": "INOUT",
                        "event": "ENTER",
                        "signal": "enter",
                        "code": "10",
                        "state": "0",
                        "boot_id": "a1b2c3d4",
                        "event_seq": "41",
                        "event_ts_ms": "100",
                    },
                ),
            )
            rid = session.commands[0].command.rsplit("=", 1)[1]
            legacy = {
                "device": address,
                "type": "DEBUG",
                "event": "ENTER",
                "value": 10,
            }
            await daemon.handle_frame(
                address,
                parse_frame(
                    build_frame(
                        address,
                        "REPORT",
                        json.dumps(legacy, separators=(",", ":")).encode(),
                    )
                ),
            )
            ack = report_frame(
                address,
                "src=INOUT,event=CONFIRM_ACK,schema=2,bid=a1b2c3d4,"
                f"cid=41,rid={rid},state=in,source=slimhub,applied=1,"
                "changed=0,reason=already_applied,legacy=0,ts=101",
                {
                    "src": "INOUT",
                    "event": "CONFIRM_ACK",
                    "schema": "2",
                    "bid": "a1b2c3d4",
                    "cid": "41",
                    "rid": rid,
                    "state": "in",
                    "source": "slimhub",
                    "applied": "1",
                    "changed": "0",
                    "reason": "already_applied",
                    "legacy": "0",
                    "ts": "101",
                },
            )
            await daemon.handle_frame(address, ack)
            await daemon.handle_frame(address, ack)

            debug_path = next(
                paths.data_dir.glob("*/*/*/inference/debugstr/*.txt")
            )
            self.assertEqual(
                len(debug_path.read_text(encoding="utf-8").splitlines()),
                1,
            )
            self.assertEqual(
                len(paths.display_path.read_text(encoding="utf-8").splitlines()),
                1,
            )
            self.assertEqual(
                daemon.dean_contract.snapshot(address)["occupancy"],
                "IN",
            )

    async def test_authoritative_handoff_commits_a_exit_before_b_enter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mac_a = "AA:BB:CC:DD:EE:01"
            mac_b = "AA:BB:CC:DD:EE:02"
            paths = AppPaths.from_base(tmpdir)
            daemon = SlimHubDaemon(paths=paths)
            daemon.config_store.set_field(mac_a, "location", "TOILET")
            daemon.config_store.set_field(mac_b, "location", "LIVING")
            command_capture: list[tuple[str, str]] = []
            session_a = FakeSession(mac_a, command_capture)
            session_b = FakeSession(mac_b, command_capture)
            await daemon.registry.add(session_a)
            await daemon.registry.add(session_b)

            async def send_fields(address: str, **fields: object) -> None:
                values = {key: str(value) for key, value in fields.items()}
                message = ",".join(
                    f"{key}={value}" for key, value in values.items()
                )
                await daemon.handle_frame(
                    address,
                    report_frame(address, message, values),
                )

            await send_fields(
                mac_a,
                src="INOUT",
                event="ENTER",
                signal="enter",
                code=10,
                state=0,
                boot_id="aaaaaaaa",
                event_seq=1,
                event_ts_ms=100,
            )
            rid_a = session_a.commands[-1].command.rsplit("=", 1)[1]
            await send_fields(
                mac_a,
                src="INOUT",
                event="CONFIRM_ACK",
                schema=2,
                bid="aaaaaaaa",
                cid=1,
                rid=rid_a,
                state="in",
                source="slimhub",
                applied=1,
                changed=1,
                reason="applied",
                legacy=0,
                ts=101,
            )
            with patch("slimhub.daemon.time.time", return_value=1_800_000_000.0):
                await daemon.handle_frame(
                    mac_a,
                    json_report_frame(
                        mac_a,
                        {
                            "device": mac_a,
                            "type": "DEBUG",
                            "event": "ENTER",
                            "value": 10,
                        },
                    ),
                )

            await send_fields(
                mac_b,
                src="INOUT",
                event="ENTER",
                signal="enter",
                code=10,
                state=0,
                boot_id="bbbbbbbb",
                event_seq=7,
                event_ts_ms=200,
            )
            self.assertEqual(command_capture[-1], ("exit", mac_a))
            self.assertEqual(len(session_b.commands), 0)
            await daemon.handle_command_result(
                session_a.commands[-1],
                True,
                None,
                1_800_000_001.0,
            )
            await send_fields(
                mac_a,
                src="INOUT",
                event="CONFIRM_ACK",
                schema=2,
                bid="aaaaaaaa",
                cid=2,
                rid="00000000",
                state="out",
                source="slimhub",
                applied=1,
                changed=1,
                reason="applied",
                legacy=1,
                ts=201,
            )
            self.assertEqual(len(session_b.commands), 0)
            await send_fields(
                mac_a,
                src="INOUT",
                event="SEQUENCE",
                schema=2,
                result="EXIT_SYNC",
                event_id="D1",
                boot_id="aaaaaaaa",
                event_seq=2,
                event_ts_ms=202,
            )
            await daemon.flush_report_reorder_buffer()
            self.assertEqual(len(session_b.commands), 0)
            with patch("slimhub.daemon.time.time", return_value=1_800_000_002.0):
                await daemon.handle_frame(
                    mac_a,
                    json_report_frame(
                        mac_a,
                        {
                            "device": mac_a,
                            "type": "DEBUG",
                            "event": "EXIT",
                            "value": 20,
                        },
                    ),
                )

            self.assertEqual(len(session_b.commands), 1)
            self.assertTrue(
                session_b.commands[0].command.startswith(
                    "inout_confirm,bid=bbbbbbbb,cid=7,state=in,rid="
                )
            )
            rid_b = session_b.commands[0].command.rsplit("=", 1)[1]
            await send_fields(
                mac_b,
                src="INOUT",
                event="CONFIRM_ACK",
                schema=2,
                bid="bbbbbbbb",
                cid=7,
                rid=rid_b,
                state="in",
                source="slimhub",
                applied=1,
                changed=1,
                reason="applied",
                legacy=0,
                ts=203,
            )
            await send_fields(
                mac_b,
                src="INOUT",
                event="SEQUENCE",
                schema=2,
                result="ENTER_CONFIRMED",
                event_id="D0",
                boot_id="bbbbbbbb",
                event_seq=7,
                event_ts_ms=204,
            )
            await daemon.flush_report_reorder_buffer()
            with patch("slimhub.daemon.time.time", return_value=1_800_000_003.0):
                await daemon.handle_frame(
                    mac_b,
                    json_report_frame(
                        mac_b,
                        {
                            "device": mac_b,
                            "type": "DEBUG",
                            "event": "ENTER",
                            "value": 10,
                        },
                    ),
                )

            self.assertEqual(
                command_capture,
                [
                    (session_a.commands[0].command, mac_a),
                    ("exit", mac_a),
                    (session_b.commands[0].command, mac_b),
                ],
            )
            self.assertEqual(
                daemon.dean_contract.home_snapshot()["confirmed_occupant"],
                mac_b,
            )
            display = paths.display_path.read_text(encoding="utf-8")
            markers = [
                "TOILET [EVENT] - ENTER value: 10",
                "TOILET [EVENT] - EXIT value: 20",
                "LIVING [EVENT] - ENTER value: 10",
            ]
            positions = [display.index(marker) for marker in markers]
            self.assertEqual(positions, sorted(positions))
            for marker in markers:
                self.assertEqual(display.count(marker), 1)
            debug_by_mac: dict[str, list[str]] = {}
            for path in paths.data_dir.glob("*/*/*/inference/debugstr/*.txt"):
                debug_by_mac[path.parents[2].name] = [
                    json.loads(line)["event"]
                    for line in path.read_text(encoding="utf-8").splitlines()
                ]
            self.assertEqual(debug_by_mac[mac_a], ["ENTER", "EXIT"])
            self.assertEqual(debug_by_mac[mac_b], ["ENTER"])
            audit = "".join(
                path.read_text(encoding="utf-8")
                for path in (paths.programdata_dir / "reports").glob("*.jsonl")
            )
            for marker in (
                "inout_handoff_exit_applied",
                "inout_handoff_exit_sync",
                "inout_handoff_exit_legacy_committed",
                "inout_handoff_barrier_complete",
                "inout_handoff_complete",
                "EXIT_SYNC",
                "ENTER_CONFIRMED",
            ):
                self.assertIn(marker, audit)

    async def test_node_config_dispatch_waits_for_applied_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            await daemon.handle_frame(
                address,
                report_frame(
                    address,
                    "src=NODE,event=STATUS,bid=a1b2c3d4,authority=slimhub_confirmed,"
                    "occupancy=OUT,capture=IDLE,profile=toilet_v1,class_count=10",
                    {
                        "src": "NODE",
                        "event": "STATUS",
                        "bid": "a1b2c3d4",
                        "authority": "slimhub_confirmed",
                        "occupancy": "OUT",
                        "capture": "IDLE",
                        "profile": "toilet_v1",
                        "class_count": "10",
                    },
                ),
            )

            response = await daemon.dispatch(
                {
                    "command": "node.config.set",
                    "args": {
                        "address": address,
                        "node_location": "KITCHEN",
                        "profile": "kitchen_v1",
                    },
                }
            )

            self.assertEqual(
                session.commands[-1].command,
                "config_set,location=KITCHEN,sound_profile=kitchen_v1",
            )
            self.assertEqual(response["data"]["cached"]["profile"], "toilet_v1")

    async def test_location_sync_routes_location_only_command_to_report_mac(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            daemon.config_store.set_field(address, "location", "TOILET")
            node_fields = {
                "src": "NODE",
                "event": "STATUS",
                "bid": "1a2b3c4d",
                "occupancy": "OUT",
                "capture": "IDLE",
                "location": "LIVING",
                "config": "file_not_found",
                "semantic": "0",
            }
            config_fields = {
                "src": "CONFIG",
                "event": "STATUS",
                "bid": "1a2b3c4d",
                "location": "LIVING",
                "profile": "living_v1",
                "config": "file_not_found",
                "semantic": "0",
                "class_count": "5",
                "model": "11223344",
                "raw": "2",
            }

            for fields in (node_fields, config_fields):
                message = ",".join(
                    f"{key}={value}" for key, value in fields.items()
                )
                await daemon.handle_frame(
                    address,
                    report_frame(address, message, fields),
                )

            self.assertEqual(
                [command.command for command in session.commands],
                ["config_set,location=TOILET"],
            )
            self.assertEqual(session.commands[0].address, address)
            self.assertEqual(
                daemon.dean_contract.snapshot(address)["raw_schema"],
                2,
            )
            audit = next(
                (Path(tmpdir) / "programdata" / "reports").glob("*.jsonl")
            ).read_text(encoding="utf-8")
            self.assertIn("location_report_mismatch", audit)
            self.assertIn('"central_location": "TOILET"', audit)

    def test_json_device_match_is_case_insensitive(self) -> None:
        address = "AA:BB:CC:DD:EE:01"
        frame = json_report_frame(
            address,
            {"device": "aa:bb:cc:dd:ee:01", "type": "EVENT"},
        )

        self.assertIsNone(SlimHubDaemon._json_identity_warning(frame))

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
            self.assertEqual(timeline, {})
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

    async def test_invalid_strict_debug_value_is_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            document = {
                "device": address,
                "type": "DEBUG",
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
            invalid = next(row for row in rows if row["kind"] == "legacy_report_rejected")
            self.assertEqual(invalid["reason"], "invalid_debug_value")
            self.assertEqual(invalid["declared_length"], invalid["actual_length"])

    async def test_legacy_detected_10_does_not_assign_demo_token(self) -> None:
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

            self.assertEqual(entry.commands, [])
            self.assertEqual(living.commands, [])

    async def test_legacy_detected_20_does_not_assign_demo_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            entry = FakeSession("AA:BB:CC:DD:EE:01")
            living = FakeSession("AA:BB:CC:DD:EE:02")
            await daemon.registry.add(entry)
            await daemon.registry.add(living)
            daemon.config_store.set_field(entry.address, "location", "ENTRY")
            daemon.config_store.set_field(living.address, "location", "LIVING")

            await daemon.handle_frame(entry.address, raw_frame(entry.address, detected=10))
            await daemon.handle_frame(living.address, raw_frame(living.address, detected=20))
            await daemon.handle_frame(entry.address, raw_frame(entry.address, detected=20))

            self.assertEqual(entry.commands, [])
            self.assertEqual(living.commands, [])
            self.assertIsNone(
                daemon.dean_contract.home_snapshot()["desired_occupant"]
            )

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

    async def test_legacy_inout_report_enter_is_not_command_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            daemon.config_store.set_field(address, "location", "ENTRY")

            await daemon.handle_frame(address, inout_report_frame(address, "ENTER"))

            self.assertEqual(session.commands, [])

    async def test_legacy_report_does_not_route_feedback_by_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ble_address = "AA:BB:CC:DD:EE:01"
            frame_mac = "11:22:33:44:55:66"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(ble_address)
            await daemon.registry.add(session)

            await daemon.handle_frame(ble_address, inout_report_frame(frame_mac, "ENTER"))

            self.assertEqual(session.commands, [])

    async def test_legacy_report_enter_cannot_move_home_token(self) -> None:
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

            self.assertEqual(sent, [])

    async def test_legacy_report_exit_cannot_clear_home_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)

            await daemon.handle_frame(address, inout_report_frame(address, "ENTER"))
            await daemon.handle_frame(address, inout_report_frame(address, "EXIT"))

            self.assertEqual(session.commands, [])
            self.assertIsNone(
                daemon.dean_contract.home_snapshot()["desired_occupant"]
            )

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

    async def test_command_can_target_unique_configured_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            daemon.config_store.set_field(address, "location", "TOILET")

            response = await daemon.dispatch(
                {
                    "command": "command.send",
                    "args": {"location": "toilet", "command": "node_status"},
                }
            )

            self.assertTrue(response["ok"])
            self.assertEqual(response["data"]["address"], address)
            self.assertEqual(response["data"]["location"], "TOILET")
            self.assertEqual(session.commands[-1].command, "node_status")

    async def test_duplicate_location_blocks_command_and_lists_devices(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            first = "AA:BB:CC:DD:EE:01"
            second = "AA:BB:CC:DD:EE:02"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            daemon.config_store.set_field(first, "location", "KITCHEN")
            daemon.config_store.set_field(second, "location", "KITCHEN")

            with self.assertRaises(ValueError) as error:
                await daemon.dispatch(
                    {
                        "command": "command.send",
                        "args": {"location": "KITCHEN", "command": "enter"},
                    }
                )

            message = str(error.exception)
            self.assertIn("cannot be used for a command", message)
            self.assertIn(first, message)
            self.assertIn(second, message)

            devices = (await daemon.dispatch({"command": "devices"}))["data"]
            conflicts = [item for item in devices if item["location_conflict"]]
            self.assertEqual(len(conflicts), 2)
            self.assertEqual(
                set(conflicts[0]["location_conflict_devices"]),
                {first, second},
            )

    async def test_config_location_collision_is_numbered_and_warned(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            first = "AA:BB:CC:DD:EE:01"
            second = "AA:BB:CC:DD:EE:02"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            daemon.config_store.set_field(first, "location", "BEDROOM")

            response = await daemon.dispatch(
                {
                    "command": "config.set",
                    "args": {
                        "address": second,
                        "field": "location",
                        "value": "BEDROOM",
                    },
                }
            )

            self.assertEqual(response["data"]["location"], "BEDROOM_2")
            self.assertEqual(len(response["data"]["warnings"]), 1)
            self.assertIn(first, response["data"]["warnings"][0])

    async def test_waited_sound_capture_uses_reports_and_never_creates_central_wav(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            daemon.config_store.set_field(address, "location", "TOILET")

            request = asyncio.create_task(
                daemon.dispatch(
                    {
                        "command": "sound.capture",
                        "args": {
                            "location": "TOILET",
                            "command": "sound_start,label=pee,thr=800,max=60,silence=5",
                            "wait": True,
                            "timeout": 1.0,
                        },
                    }
                )
            )
            await asyncio.sleep(0)
            await daemon.handle_frame(
                address,
                sound_report_frame(
                    address,
                    "CAPTURE_ARMED",
                    label="pee",
                ),
            )
            await daemon.handle_frame(
                address,
                sound_report_frame(
                    address,
                    "CAPTURE_START",
                    label="pee",
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
                    complete="1",
                ),
            )

            response = await asyncio.wait_for(request, timeout=1.0)
            self.assertTrue(response["data"]["outcome"]["success"])
            self.assertFalse((Path(tmpdir) / "data" / "sound").exists())
            self.assertIsNone(daemon.estimator.snapshot()["last_address"])
            self.assertEqual(session.commands[0].command.split(",")[0], "sound_start")

    async def test_sound_status_waits_for_fresh_node_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            session = FakeSession(address)
            await daemon.registry.add(session)
            daemon.config_store.set_field(address, "location", "TOILET")

            request = asyncio.create_task(
                daemon.dispatch(
                    {
                        "command": "sound.status",
                        "args": {"location": "TOILET"},
                    }
                )
            )
            await asyncio.sleep(0)
            await daemon.handle_frame(
                address,
                sound_report_frame(
                    address,
                    "CAPTURE_STATUS",
                    state="ACTIVE",
                    label="background",
                ),
            )
            response = await asyncio.wait_for(request, timeout=1.0)

            self.assertTrue(response["data"]["fresh_report"])
            self.assertEqual(
                response["data"]["capture"]["status"]["state"],
                "ACTIVE",
            )
            self.assertEqual(session.commands[0].command, "sound_status")

    async def test_sound_inference_and_adjacent_rawdata_count_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            fields = {
                "src": "SOUND",
                "event": "INFERENCE",
                "schema": "2",
                "bid": "1a2b3c4d",
                "location": "TOILET",
                "class_index": "5",
                "class_count": "10",
                "label": "flushing",
                "semantic": "flushing",
                "confidence": "0.91",
                "model": "0cb81518",
                "source": "tflm",
                "raw": "2",
                "rms": "1420.5",
                "db": "61.2",
                "ts": "45000",
                "duration_ms": "1000",
            }
            message = ",".join(f"{key}={value}" for key, value in fields.items())

            await daemon.handle_frame(
                address,
                report_frame(address, message, fields),
            )
            await daemon.handle_frame(address, raw_sound_frame(address))

            self.assertEqual(daemon.sound_inference.inference_count(), 1)
            latest = daemon.sound_inference.snapshot(address)[0]["last_inference"]
            self.assertEqual(latest["label"], "flushing")

    async def test_malformed_sound_inference_is_diagnostic_and_daemon_continues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = "AA:BB:CC:DD:EE:01"
            daemon = SlimHubDaemon(paths=AppPaths.from_base(tmpdir))
            fields = {
                "src": "SOUND",
                "event": "INFERENCE",
                "schema": "2",
                "bid": "1a2b3c4d",
                "location": "TOILET",
                "class_index": "20",
                "class_count": "20",
                "label": "invalid",
                "semantic": "unknown",
                "confidence": "0.4",
                "model": "0cb81518",
                "source": "tflm",
                "ts": "45000",
                "duration_ms": "1000",
            }
            message = ",".join(f"{key}={value}" for key, value in fields.items())

            await daemon.handle_frame(
                address,
                report_frame(address, message, fields),
            )

            self.assertFalse(daemon.stop_event.is_set())
            self.assertEqual(daemon.sound_inference.inference_count(), 0)
            self.assertEqual(
                daemon.sound_inference.diagnostic_count(
                    "sound_inference_rejected"
                ),
                1,
            )
            audit = next(
                (Path(tmpdir) / "programdata" / "reports").glob("*.jsonl")
            ).read_text(encoding="utf-8")
            self.assertIn(address, audit)
            self.assertIn(message, audit)
            self.assertIn("less than class_count", audit)

    async def test_legacy_sequence_reports_never_emit_demo_commands(self) -> None:
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

            self.assertEqual(a.commands, [])
            self.assertEqual(b.commands, [])
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
