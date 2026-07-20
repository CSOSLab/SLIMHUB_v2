from __future__ import annotations

import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from slimhub.config import AppPaths
from slimhub.events import ReportEvent
from slimhub.protocol.nus import (
    AudioPacket,
    ReportPacket,
    build_sound_background_command,
    build_sound_start_command,
)
from slimhub.sound_capture import SoundCaptureStore


MAC = "AA:BB:CC:DD:EE:FF"
CID = 0x00AB12CD


def report_event(timestamp: float, event: str, **fields: object) -> ReportEvent:
    values = {
        "src": "SOUND",
        "event": event,
        "cid": f"{CID:08x}",
        **{key: str(value) for key, value in fields.items()},
    }
    message = ",".join(f"{key}={value}" for key, value in values.items())
    return ReportEvent(
        timestamp=timestamp,
        mac=MAC,
        source_address=MAC,
        location="TOILET",
        packet=ReportPacket(message=message, fields=values),
        payload=message.encode(),
    )


def audio_packet(sequence: int, offset: int, sample_count: int = 512) -> AudioPacket:
    pcm = struct.pack(
        f"<{sample_count}h",
        *[((index % 200) - 100) for index in range(sample_count)],
    )
    return AudioPacket(
        version=1,
        flags=1 if sequence == 0 else 0,
        encoding=1,
        channels=1,
        capture_id=CID,
        block_sequence=sequence,
        sample_offset=offset,
        sample_count=sample_count,
        data_bytes=len(pcm),
        pcm=pcm,
    )


class SoundCaptureStoreTests(unittest.TestCase):
    def capture_paths(self, base: str, label: str = "pee") -> tuple[Path, Path]:
        root = Path(base) / "data" / "sound" / MAC / label
        return root / f"{CID:08x}.wav", root / f"{CID:08x}.json"

    def arm_and_start(self, store: SoundCaptureStore) -> None:
        store.register_command(
            MAC,
            build_sound_start_command("pee", destination="ble"),
            100.0,
        )
        store.handle_report(
            report_event(
                101.0,
                "CAPTURE_ARMED",
                label="pee",
                dest="ble",
                threshold_rms=800,
                max_ms=60000,
                silence_ms=5000,
            )
        )
        store.handle_report(
            report_event(
                102.0,
                "CAPTURE_START",
                label="pee",
                dest="ble",
                sr=16000,
                bits=16,
                channels=1,
            )
        )

    def test_complete_capture_writes_pcm16_wav_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundCaptureStore(AppPaths.from_base(tmpdir))
            self.arm_and_start(store)

            self.assertTrue(store.handle_audio(MAC, MAC, audio_packet(0, 0), 102.1))
            store.handle_report(
                report_event(
                    103.0,
                    "CAPTURE_DONE",
                    samples=512,
                    blocks=1,
                    queue_drop=0,
                    ble_drop=0,
                    max_rms=8200,
                    reason="command_stop",
                )
            )

            wav_path, manifest_path = self.capture_paths(tmpdir)
            with wave.open(str(wav_path), "rb") as recorded:
                self.assertEqual(recorded.getframerate(), 16000)
                self.assertEqual(recorded.getnchannels(), 1)
                self.assertEqual(recorded.getsampwidth(), 2)
                self.assertEqual(recorded.getnframes(), 512)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["termination"], "command_stop")
            self.assertEqual(manifest["audio"]["received_samples"], 512)
            self.assertEqual(manifest["audio"]["received_blocks"], 1)
            self.assertEqual(manifest["audio"]["reported_samples"], 512)
            self.assertEqual(manifest["audio"]["reported_blocks"], 1)
            self.assertIn("CAPTURE_ARMED", manifest["reports"])
            self.assertIn("CAPTURE_START", manifest["reports"])
            self.assertIn("CAPTURE_DONE", manifest["reports"])

    def test_sequence_and_sample_gap_marks_manifest_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundCaptureStore(AppPaths.from_base(tmpdir))
            self.arm_and_start(store)
            store.handle_audio(MAC, MAC, audio_packet(0, 0), 102.1)
            store.handle_audio(MAC, MAC, audio_packet(2, 1024), 102.2)
            store.handle_report(
                report_event(
                    103.0,
                    "CAPTURE_DONE",
                    samples=1536,
                    blocks=3,
                    queue_drop=0,
                    ble_drop=0,
                    reason="command_stop",
                )
            )

            _, manifest_path = self.capture_paths(tmpdir)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            kinds = {item["kind"] for item in manifest["audio"]["missing_ranges"]}
            self.assertFalse(manifest["complete"])
            self.assertIn("block_sequence", kinds)
            self.assertIn("sample_offset", kinds)
            self.assertIn("reported_sample_count", kinds)
            self.assertIn("reported_block_count", kinds)

    def test_disconnect_finalizes_recoverable_incomplete_wav(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundCaptureStore(AppPaths.from_base(tmpdir))
            self.arm_and_start(store)
            store.handle_audio(MAC, MAC, audio_packet(0, 0), 102.1)

            store.handle_disconnect(MAC, 103.0)

            wav_path, manifest_path = self.capture_paths(tmpdir)
            with wave.open(str(wav_path), "rb") as recorded:
                self.assertEqual(recorded.getnframes(), 512)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["termination"], "disconnect")
            self.assertEqual(manifest["state"], "INCOMPLETE")

    def test_missing_done_is_recovered_after_active_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundCaptureStore(AppPaths.from_base(tmpdir))
            self.arm_and_start(store)
            store.handle_audio(MAC, MAC, audio_packet(0, 0), 102.1)

            recovered = store.recover_timeouts(173.0)

            _, manifest_path = self.capture_paths(tmpdir)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(recovered, 1)
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["termination"], "timeout")

    def test_unsolicited_audio_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundCaptureStore(AppPaths.from_base(tmpdir))

            accepted = store.handle_audio(MAC, MAC, audio_packet(0, 0), 100.0)

            self.assertFalse(accepted)
            self.assertFalse((Path(tmpdir) / "data" / "sound").exists())

    def test_background_command_always_uses_background_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SoundCaptureStore(AppPaths.from_base(tmpdir))
            store.register_command(
                MAC,
                build_sound_background_command(destination="ble", max_seconds=600),
                100.0,
            )
            store.handle_report(
                report_event(
                    101.0,
                    "CAPTURE_START",
                    label="malicious_report_label",
                    dest="ble",
                )
            )
            store.handle_audio(MAC, MAC, audio_packet(0, 0), 101.1)
            store.handle_disconnect(MAC, 102.0)

            wav_path, manifest_path = self.capture_paths(tmpdir, "background")
            self.assertTrue(wav_path.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["label"], "background")
            self.assertEqual(manifest["command"]["threshold_rms"], 0)
            self.assertEqual(manifest["command"]["silence_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
