from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slimhub.events import ReportEvent
from slimhub.multimodal import DeploymentManifestStore, MultimodalReportStore
from slimhub.protocol.nus import ReportPacket


class MultimodalReplayTests(unittest.TestCase):
    def test_env_sound_adl_fixture_replays_to_one_final_session(self) -> None:
        fixture = Path(__file__).with_name("fixtures") / "multimodal_adl_replay.jsonl"
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MultimodalReportStore(
                DeploymentManifestStore(Path(tmpdir) / "deployment_manifest.json")
            )
            for line in fixture.read_text(encoding="utf-8").splitlines():
                fields = {key: str(value) for key, value in json.loads(line).items()}
                message = ",".join(f"{key}={value}" for key, value in fields.items())
                event = ReportEvent(
                    timestamp=float(fields["event_ts_ms"]) / 1000,
                    receipt_timestamp=float(fields["event_ts_ms"]) / 1000,
                    normalized_timestamp=float(fields["event_ts_ms"]) / 1000,
                    mac="AA:BB:CC:DD:EE:01",
                    source_address="AA:BB:CC:DD:EE:01",
                    location="TOILET",
                    packet=ReportPacket(message, fields),
                    payload=message.encode(),
                )
                if fields["src"] == "INOUT":
                    store.handle_inout(event)
                else:
                    store.handle(event)

            session = store.snapshot()["sessions"]["AA:BB:CC:DD:EE:01/node-a/7"]
            self.assertEqual([record["event"] for record in session["records"][:2]], ["ENV", "SOUND"])
            self.assertEqual(session["final"]["event"], "COMPLETE")
            self.assertEqual(session["d0"]["inout_event_seq"], 41)
            self.assertEqual(session["d1"]["inout_event_seq"], 88)


if __name__ == "__main__":
    unittest.main()
