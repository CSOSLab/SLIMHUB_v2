from __future__ import annotations

import json
import unittest
from pathlib import Path

from slimhub.dean_contract import DeanContractStore
from slimhub.events import RawDataEvent, ReportEvent
from slimhub.protocol.nus import RawDataPacket, ReportPacket


class TwoNodeReplayTests(unittest.TestCase):
    def test_fixture_replay_converges_to_one_home_token(self) -> None:
        fixture = Path(__file__).with_name("fixtures") / "two_node_inout_replay.jsonl"
        rids = iter((0x11, 0x22, 0x33))
        store = DeanContractStore(rid_factory=lambda: next(rids))
        commands: list[tuple[str, str]] = []

        for line in fixture.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item["kind"] == "raw":
                packet = RawDataPacket(
                    flag_human_presence=1,
                    detected=item["detected"],
                    flag_env=0,
                    temperature_c=0.0,
                    humidity=0,
                    iaq=0,
                    eco2=0,
                    bvoc=0,
                    accuracy=0,
                    flag_sound=0,
                    sound=[0] * 16,
                    is_pir_human_detection_event=True,
                )
                result = store.handle_raw(
                    RawDataEvent(
                        item["receipt_ts"],
                        item["mac"],
                        item["location"],
                        packet,
                        b"",
                    )
                )
            else:
                fields = {
                    key: str(value)
                    for key, value in item.items()
                    if key not in {"kind", "mac", "location", "receipt_ts"}
                }
                fields["location"] = item["location"]
                message = ",".join(f"{key}={value}" for key, value in fields.items())
                result = store.handle_report(
                    ReportEvent(
                        timestamp=item["receipt_ts"],
                        receipt_timestamp=item["receipt_ts"],
                        mac=item["mac"],
                        source_address=item["mac"],
                        location=item["location"],
                        packet=ReportPacket(message, fields),
                        payload=message.encode(),
                    )
                )
            commands.extend((command.command, command.address) for command in result)

        self.assertEqual(
            commands,
            [
                (
                    "inout_confirm,bid=aaaa0001,cid=1,state=in,rid=11",
                    "AA:BB:CC:DD:EE:01",
                ),
                (
                    "inout_confirm,bid=aaaa0001,cid=2,state=out,rid=22",
                    "AA:BB:CC:DD:EE:01",
                ),
                (
                    "inout_confirm,bid=bbbb0002,cid=7,state=in,rid=33",
                    "AA:BB:CC:DD:EE:02",
                ),
            ],
        )
        self.assertEqual(
            store.home_snapshot()["confirmed_occupant"],
            "AA:BB:CC:DD:EE:02",
        )


if __name__ == "__main__":
    unittest.main()
