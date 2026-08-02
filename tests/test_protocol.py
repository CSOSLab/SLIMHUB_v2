from __future__ import annotations

import base64
import json
import struct
import unittest
from pathlib import Path

from slimhub.protocol.nus import (
    END_FLAG,
    FrameAssembler,
    IgnoredPacket,
    PacketParseError,
    RawDataPacket,
    ReportPacket,
    build_command_frame,
    build_frame,
    build_record_command,
    build_sound_background_command,
    build_sound_auto_command,
    build_sound_start_command,
    parse_frame,
)


class ProtocolTests(unittest.TestCase):
    def test_location_only_config_set_targets_node_without_profile(self) -> None:
        frame = build_command_frame(
            "AA:BB:CC:DD:EE:FF",
            "config_set,location=TOILET",
        )

        payload_len = int.from_bytes(frame[14:16], byteorder="little")
        self.assertEqual(
            frame[16 : 16 + payload_len],
            b"config_set,location=TOILET",
        )

    def command_payload(self, command: str) -> bytes:
        frame = build_command_frame("AA:BB:CC:DD:EE:FF", command)
        payload_len = int.from_bytes(frame[14:16], byteorder="little")
        return frame[16 : 16 + payload_len]

    @staticmethod
    def audio_payload(
        *,
        capture_id: int = 0x00AB12CD,
        block_sequence: int = 0,
        sample_offset: int = 0,
        sample_count: int = 512,
    ) -> bytes:
        pcm = struct.pack(
            f"<{sample_count}h",
            *[((index % 200) - 100) for index in range(sample_count)],
        )
        return (
            struct.pack(
                "<BBBBIIIHH",
                1,
                1 if block_sequence == 0 else 0,
                1,
                1,
                capture_id,
                block_sequence,
                sample_offset,
                sample_count,
                len(pcm),
            )
            + pcm
        )

    def test_assembler_handles_split_frames(self) -> None:
        frame = build_frame("AA:BB:CC:DD:EE:FF", "ALERT", b"hello")
        assembler = FrameAssembler()

        self.assertEqual(assembler.push(frame[:4]), [])
        self.assertEqual(assembler.push(frame[4:12]), [])
        self.assertEqual(assembler.push(frame[12:]), [frame])

    def test_assembler_reassembles_mtu23_header_payload_and_crlf_splits(self) -> None:
        frame = build_frame(
            "AA:BB:CC:DD:EE:FF",
            "REPORT",
            b"src=EVENT,event=ENV,analysis_seq=51,event_id=E1",
        )
        assembler = FrameAssembler()
        chunks = [frame[:3], frame[3:16], frame[16:-1], frame[-1:]]
        frames = []
        for chunk in chunks:
            frames.extend(assembler.push(chunk))

        self.assertEqual(frames, [frame])
        self.assertEqual(parse_frame(frames[0]).parsed.fields["event_id"], "E1")

    def test_assembler_reassembles_mtu247_stream_with_concatenated_frames(self) -> None:
        first = build_frame("AA:BB:CC:DD:EE:01", "ALERT", b"one")
        second = build_frame(
            "AA:BB:CC:DD:EE:02",
            "REPORT",
            b"src=ADL,event=COMPLETE,analysis_seq=9",
        )
        stream = first + second
        assembler = FrameAssembler()

        frames = assembler.push(stream[:23]) + assembler.push(stream[23:247]) + assembler.push(stream[247:])

        self.assertEqual(frames, [first, second])

    def test_assembler_reassembles_fragmented_json_and_coalesced_csv_reports(self) -> None:
        document = {
            "device": "AA:BB:CC:DD:EE:01",
            "type": "EVENT",
            "event": "ENTER",
            "value": 10,
            "schema": 2,
            "bid": "12ab34cd",
            "eid": 41,
            "ts": 840000,
        }
        json_frame = build_frame(
            "AA:BB:CC:DD:EE:01",
            "REPORT",
            ("  " + json.dumps(document)).encode(),
        )
        csv_frame = build_frame(
            "AA:BB:CC:DD:EE:01",
            "REPORT",
            b"src=EVENT,event=ENV,schema=2,bid=12ab34cd,aid=42,ts=840100",
        )
        assembler = FrameAssembler()

        frames = []
        for chunk in (json_frame[:7], json_frame[7:-1], json_frame[-1:] + csv_frame):
            frames.extend(assembler.push(chunk))

        self.assertEqual(frames, [json_frame, csv_frame])
        json_packet = parse_frame(frames[0]).parsed
        csv_packet = parse_frame(frames[1]).parsed
        self.assertEqual(json_packet.format, "json")
        self.assertEqual(json_packet.document, document)
        self.assertEqual(csv_packet.format, "csv")
        self.assertEqual(csv_packet.fields["src"], "EVENT")

    def test_recorded_notifications_restore_audio_report_and_rawdata_in_order(self) -> None:
        fixture_path = (
            Path(__file__).parent / "fixtures" / "sound_audio_notifications.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        assembler = FrameAssembler()
        frame_bytes = []
        for encoded in fixture["notification_chunks_base64"]:
            frame_bytes.extend(assembler.push(base64.b64decode(encoded)))

        frames = [parse_frame(data) for data in frame_bytes[:2]]

        self.assertEqual(
            [frame.packet_type for frame in frames],
            ["AUDIO", "REPORT"],
        )
        self.assertIsInstance(frames[0].parsed, IgnoredPacket)
        self.assertEqual(frames[0].parsed.packet_type, "AUDIO")
        self.assertEqual(frames[1].parsed.fields["event"], "CAPTURE_DONE")
        with self.assertRaisesRegex(PacketParseError, "one active producer"):
            parse_frame(frame_bytes[2])

    def test_assembler_discards_bad_crlf_and_resynchronizes_to_next_frame(self) -> None:
        valid = build_frame("AA:BB:CC:DD:EE:FF", "ALERT", b"ready")
        malformed = valid[:-2] + b"\x00\x00"
        assembler = FrameAssembler()

        frames = assembler.push(malformed + valid)

        self.assertEqual(frames, [valid])

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
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            1,
            *range(-8, 8),
        )
        frame = parse_frame(build_frame("AA:BB:CC:DD:EE:FF", "RAWDATA", payload))

        self.assertIsInstance(frame.parsed, RawDataPacket)
        self.assertEqual(frame.mac, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(frame.parsed.detected, 0)
        self.assertEqual(frame.parsed.temperature_c, 0)
        self.assertEqual(frame.parsed.sound[0], -8)

    def test_rawdata_rejects_mixed_producers_and_inactive_cached_data(self) -> None:
        mixed = struct.pack("<BB7HB16b", 1, 1, 1, 2350, 55, 100, 450, 7, 3, 0, *([0] * 16))
        pir_with_sound = struct.pack(
            "<BB7HB16b",
            1,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            *([-128] * 16),
        )

        with self.assertRaises(PacketParseError):
            parse_frame(build_frame("AA:BB:CC:DD:EE:FF", "RAWDATA", mixed))
        with self.assertRaises(PacketParseError):
            parse_frame(
                build_frame("AA:BB:CC:DD:EE:FF", "RAWDATA", pir_with_sound)
            )

    def test_rawdata_accepts_dean_inout_candidate_codes(self) -> None:
        for detected in (10, 20):
            with self.subTest(detected=detected):
                payload = struct.pack(
                    "<BB7HB16b",
                    1,
                    detected,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    *([0] * 16),
                )
                packet = parse_frame(
                    build_frame(
                        "AA:BB:CC:DD:EE:FF",
                        "RAWDATA",
                        payload,
                    )
                ).parsed
                self.assertEqual(packet.detected, detected)

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

    def test_legacy_audio_and_wavfile_frames_are_ignored(self) -> None:
        frame = parse_frame(
            build_frame(
                "AA:BB:CC:DD:EE:FF",
                "AUDIO",
                self.audio_payload(),
            )
        )

        wavfile = parse_frame(
            build_frame("AA:BB:CC:DD:EE:FF", "WAVFILE", b"legacy")
        )

        self.assertIsInstance(frame.parsed, IgnoredPacket)
        self.assertEqual(frame.parsed.packet_type, "AUDIO")
        self.assertEqual(frame.parsed.payload_bytes, len(self.audio_payload()))
        self.assertIsInstance(wavfile.parsed, IgnoredPacket)
        self.assertEqual(wavfile.parsed.packet_type, "WAVFILE")

    def test_csv_report_keeps_first_routing_source_and_preserves_duplicate_metric(self) -> None:
        payload = b"src=ADL,event=POP,schema=2,src=3,score=98"

        packet = parse_frame(
            build_frame("AA:BB:CC:DD:EE:FF", "REPORT", payload)
        ).parsed

        self.assertEqual(packet.fields["src"], "ADL")
        self.assertEqual(packet.duplicate_fields, {"src": ["3"]})

    def test_json_report_preserves_unknown_keys_and_parse_errors(self) -> None:
        document = {
            "device": "AA:BB:CC:DD:EE:FF",
            "type": "INFERENCE",
            "status": "POP",
            "future_detail": {"weight": 3},
        }
        parsed = parse_frame(
            build_frame("AA:BB:CC:DD:EE:FF", "REPORT", json.dumps(document).encode())
        ).parsed
        malformed = parse_frame(
            build_frame("AA:BB:CC:DD:EE:FF", "REPORT", b'{"type":"EVENT"')
        ).parsed
        non_object = parse_frame(
            build_frame("AA:BB:CC:DD:EE:FF", "REPORT", b'["not","an","object"]')
        ).parsed

        self.assertEqual(parsed.format, "json")
        self.assertEqual(parsed.document["future_detail"], {"weight": 3})
        self.assertIsNone(parsed.parse_error)
        self.assertEqual(malformed.format, "json")
        self.assertIn("invalid_json", malformed.parse_error)
        self.assertEqual(malformed.message, '{"type":"EVENT"')
        self.assertEqual(non_object.format, "json")
        self.assertEqual(non_object.parse_error, "json_report_must_be_an_object")

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
        frame = build_command_frame(
            "AA:BB:CC:DD:EE:FF",
            "inout_confirm,bid=a1b2c3d4,cid=41,state=in,rid=1",
        )

        self.assertEqual(frame[:6], bytes.fromhex("AABBCCDDEEFF"))
        self.assertEqual(frame[6:14], b"COMMAND\x00")
        self.assertIn(b"inout_confirm", frame)

    def test_inout_confirm_is_strict_and_old_sync_is_rejected(self) -> None:
        self.assertEqual(
            self.command_payload(
                "inout_confirm,bid=A1B2,cid=41,state=out,rid=0x2"
            ),
            b"inout_confirm,bid=a1b2,cid=41,state=out,rid=2",
        )
        for command in (
            "enter",
            "exit",
            "inout_sync,bid=a1,state=in,rid=1",
            "inout_confirm,bid=0,cid=1,state=in,rid=1",
            "inout_confirm,bid=a1,cid=0,state=in,rid=1",
            "inout_confirm,bid=a1,cid=1,state=in,rid=0",
            "inout_confirm,bid=a1,cid=1,state=in,rid=1,extra=1",
            "inout_confirm,bid=a1,bid=a2,cid=1,state=in,rid=1",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                build_command_frame("AA:BB:CC:DD:EE:FF", command)

    def test_dean_confirm_example_is_one_73_byte_frame(self) -> None:
        frame = build_command_frame(
            "90:E5:B1:D1:22:6A",
            "inout_confirm,bid=12ab34cd,cid=41,state=in,rid=deadbeef",
        )

        self.assertEqual(len(frame), 73)
        self.assertEqual(frame[:6], bytes.fromhex("90E5B1D1226A"))
        self.assertEqual(frame[6:14], b"COMMAND\x00")
        self.assertEqual(frame[14:16], b"\x37\x00")
        self.assertEqual(frame[-2:], b"\r\n")

    def test_record_command_frame_builds_record_payload(self) -> None:
        self.assertEqual(self.command_payload("record"), b"record")

    def test_record_seconds_command_frame_builds_duration_payload(self) -> None:
        self.assertEqual(
            self.command_payload(build_record_command(15)),
            b"record:15",
        )

    def test_record_stop_command_frame_builds_stop_payload(self) -> None:
        self.assertEqual(self.command_payload("record_stop"), b"record_stop")

    def test_sound_start_command_frame_builds_exact_payload(self) -> None:
        command = build_sound_start_command(
            "pee",
            threshold_rms=1200,
            max_seconds=90,
            silence_seconds=5,
        )

        self.assertEqual(
            self.command_payload(command),
            b"sound_start,label=pee,thr=1200,max=90,silence=5",
        )

    def test_background_command_is_fixed_ungated_background_preset(self) -> None:
        command = build_sound_background_command(max_seconds=600)

        self.assertEqual(command, "sound_bg,max=600")
        self.assertNotIn("label=", command)
        self.assertNotIn("thr=", command)

    def test_sound_command_rejects_path_traversal_and_ranges(self) -> None:
        invalid_calls = (
            lambda: build_sound_start_command("../pee"),
            lambda: build_sound_start_command("x" * 25),
            lambda: build_sound_start_command("pee", threshold_rms=32768),
            lambda: build_sound_start_command("pee", max_seconds=0),
            lambda: build_sound_start_command("pee", silence_seconds=61),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

        self.assertIn(
            "silence=0",
            build_sound_start_command("pee", threshold_rms=0, silence_seconds=5),
        )

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

    def test_report_payload_boundaries_and_embedded_nul(self) -> None:
        prefix = b"src=EVENT,event=ENV,schema=2,"
        payload_256 = prefix + b"x" * (256 - len(prefix))
        payload_257 = payload_256 + b"x"

        parsed = parse_frame(
            build_frame("AA:BB:CC:DD:EE:FF", "REPORT", payload_256)
        )

        self.assertEqual(parsed.packet_length, 256)
        with self.assertRaisesRegex(PacketParseError, "exceeds 256"):
            parse_frame(build_frame("AA:BB:CC:DD:EE:FF", "REPORT", payload_257))
        with self.assertRaisesRegex(PacketParseError, "embedded NUL"):
            parse_frame(
                build_frame(
                    "AA:BB:CC:DD:EE:FF",
                    "REPORT",
                    b"src=NODE,event=STATUS\x00",
                )
            )

    def test_report_aliases_are_normalized_without_dropping_original_fields(self) -> None:
        parsed = parse_frame(
            build_frame(
                "AA:BB:CC:DD:EE:FF",
                "REPORT",
                (
                    b"src=INOUT,event=ENTER,boot_id=a1b2c3d4,"
                    b"event_seq=7,event_ts_ms=123,future=kept"
                ),
            )
        ).parsed

        self.assertEqual(parsed.fields["bid"], "a1b2c3d4")
        self.assertEqual(parsed.fields["cid"], "7")
        self.assertEqual(parsed.fields["timestamp"], "123")
        self.assertEqual(parsed.fields["future"], "kept")
        self.assertEqual(parsed.fields["boot_id"], "a1b2c3d4")

    def test_command_payload_over_128_bytes_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "1-128 bytes"):
            build_command_frame("AA:BB:CC:DD:EE:FF", "x" * 129)

    def test_sound_auto_uses_node_thresholds_unless_both_overrides_are_given(self) -> None:
        default = build_sound_auto_command()
        override = build_sound_auto_command(open_db=60, close_db=55)

        self.assertEqual(default, "sound_auto,max=300,silence=20")
        self.assertNotIn("open_db", default)
        self.assertEqual(
            override,
            "sound_auto,max=300,silence=20,open_db=60,close_db=55",
        )
        with self.assertRaisesRegex(ValueError, "requires both"):
            build_sound_auto_command(open_db=60)


if __name__ == "__main__":
    unittest.main()
