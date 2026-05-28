from __future__ import annotations

import struct
import unittest

from slimhub.protocol.nus import (
    END_FLAG,
    FrameAssembler,
    PacketParseError,
    RawDataPacket,
    build_command_frame,
    build_frame,
    parse_frame,
)


class ProtocolTests(unittest.TestCase):
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

    def test_command_frame_uses_target_mac_and_command_packet_type(self) -> None:
        frame = build_command_frame("AA:BB:CC:DD:EE:FF", "enter")

        self.assertEqual(frame[:6], bytes.fromhex("AABBCCDDEEFF"))
        self.assertEqual(frame[6:14], b"COMMAND\x00")
        self.assertIn(b"enter", frame)


if __name__ == "__main__":
    unittest.main()
