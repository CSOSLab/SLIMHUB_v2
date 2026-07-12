from __future__ import annotations

import struct
import unittest

from slimhub.protocol.nus import (
    END_FLAG,
    FrameAssembler,
    PacketParseError,
    RawDataPacket,
    ReportPacket,
    build_command_frame,
    build_frame,
    build_record_command,
    parse_frame,
)


class ProtocolTests(unittest.TestCase):
    def command_payload(self, command: str) -> bytes:
        frame = build_command_frame("AA:BB:CC:DD:EE:FF", command)
        payload_len = int.from_bytes(frame[14:16], byteorder="little")
        return frame[16 : 16 + payload_len]

    def test_assembler_handles_split_frames(self) -> None:
        frame = build_frame("AA:BB:CC:DD:EE:FF", "ALERT", b"hello")
        assembler = FrameAssembler()

        self.assertEqual(assembler.push(frame[:4]), [])
        self.assertEqual(assembler.push(frame[4:12]), [])
        self.assertEqual(assembler.push(frame[12:]), [frame])

    def test_malformed_end_flag_fails(self) -> None:
        frame = build_frame("AA:BB:CC:DD:EE:FF", "ALERT", b"hello")
        bad_frame = frame[: -len(END_FLAG)] + b"\x00\x00"

        with self.assertRaises(PacketParseError):
            parse_frame(bad_frame)

    def test_malformed_length_fails(self) -> None:
        frame = bytearray(build_frame("AA:BB:CC:DD:EE:FF", "ALERT", b"hello"))
        frame[14:16] = (99).to_bytes(2, byteorder="little")

        with self.assertRaises(PacketParseError):
            parse_frame(bytes(frame))

    def test_rawdata_frame_parses_payload(self) -> None:
        payload = struct.pack(
            "<BB7HB16b",
            1,
            1,
            1,
            2350,
            55,
            100,
            450,
            7,
            3,
            1,
            *range(-8, 8),
        )
        frame = parse_frame(build_frame("AA:BB:CC:DD:EE:FF", "RAWDATA", payload))

        self.assertIsInstance(frame.parsed, RawDataPacket)
        self.assertEqual(frame.mac, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(frame.parsed.detected, 1)
        self.assertEqual(frame.parsed.temperature_c, 23.5)
        self.assertEqual(frame.parsed.sound[0], -8)

    def test_alert_frame_parses_text(self) -> None:
        frame = parse_frame(build_frame("AA:BB:CC:DD:EE:FF", "ALERT", b"ready"))

        self.assertEqual(frame.packet_type, "ALERT")
        self.assertEqual(frame.parsed.message, "ready")

    def test_sound_report_frame_parses_key_value_payload(self) -> None:
        payload = b"src=SOUND,event=RECORD_START,path=SOUND/001.wav,max_ms=30000"
        frame = parse_frame(build_frame("AA:BB:CC:DD:EE:FF", "REPORT", payload))

        self.assertIsInstance(frame.parsed, ReportPacket)
        self.assertEqual(frame.packet_type, "REPORT")
        self.assertEqual(frame.parsed.fields["src"], "SOUND")
        self.assertEqual(frame.parsed.fields["event"], "RECORD_START")
        self.assertEqual(frame.parsed.fields["path"], "SOUND/001.wav")

    def test_inout_report_frame_parses_state_payload(self) -> None:
        payload = (
            b"src=INOUT,event=ENTER,signal=enter,code=10,pir=1,"
            b"radar=1,dist_cm=75,state=inside_moving,reason=radar_confirmed"
        )
        frame = parse_frame(build_frame("AA:BB:CC:DD:EE:FF", "REPORT", payload))

        self.assertIsInstance(frame.parsed, ReportPacket)
        self.assertEqual(frame.parsed.fields["src"], "INOUT")
        self.assertEqual(frame.parsed.fields["event"], "ENTER")
        self.assertEqual(frame.parsed.fields["code"], "10")
        self.assertEqual(frame.parsed.fields["state"], "inside_moving")

    def test_command_frame_uses_target_mac_and_command_packet_type(self) -> None:
        frame = build_command_frame("AA:BB:CC:DD:EE:FF", "enter")

        self.assertEqual(frame[:6], bytes.fromhex("AABBCCDDEEFF"))
        self.assertEqual(frame[6:14], b"COMMAND\x00")
        self.assertIn(b"enter", frame)

    def test_exit_command_frame_builds_exit_payload(self) -> None:
        self.assertEqual(self.command_payload("exit"), b"exit")

    def test_command_frame_maps_legacy_enter_to_enter(self) -> None:
        frame = build_command_frame("AA:BB:CC:DD:EE:FF", "strong_enter")

        self.assertIn(b"enter", frame)
        self.assertNotIn(b"strong_enter", frame)

    def test_record_command_frame_builds_record_payload(self) -> None:
        self.assertEqual(self.command_payload("record"), b"record")

    def test_record_seconds_command_frame_builds_duration_payload(self) -> None:
        self.assertEqual(
            self.command_payload(build_record_command(15)),
            b"record:15",
        )

    def test_record_stop_command_frame_builds_stop_payload(self) -> None:
        self.assertEqual(self.command_payload("record_stop"), b"record_stop")

    def test_record_seconds_rejects_invalid_values(self) -> None:
        for command in ("record:0", "record:301", "record:abc"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "record seconds"):
                    build_command_frame("AA:BB:CC:DD:EE:FF", command)

        with self.assertRaisesRegex(ValueError, "record seconds"):
            build_record_command(0)

    def test_command_frame_rejects_unknown_command(self) -> None:
        with self.assertRaisesRegex(ValueError, "command must be one of"):
            build_command_frame("AA:BB:CC:DD:EE:FF", "stay")


if __name__ == "__main__":
    unittest.main()
