from __future__ import annotations

import tempfile
import unittest

from slimhub.config import AppPaths
from slimhub.events import CommandEvent
from slimhub.power_shadow import (
    ABSENT_SLEEP,
    DISCONNECTED,
    MIC_ASSISTED_ACTIVE,
    PIR_TRIGGER_VERIFY,
    RADAR_CONFIRMED_ACTIVE,
    SLEEP_READY,
    ShadowPowerState,
)
from slimhub.protocol.nus import RawDataPacket, ReportPacket


ADDRESS = "AA:BB:CC:DD:EE:FF"


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


class ShadowPowerStateTests(unittest.TestCase):
    def test_initial_device_state_is_absent_sleep(self) -> None:
        shadow = ShadowPowerState()

        self.assertEqual(shadow.snapshot(ADDRESS)["state"], ABSENT_SLEEP)

    def test_rawdata_human_detected_moves_to_verify_active(self) -> None:
        shadow = ShadowPowerState()

        snapshot = shadow.update_rawdata(ADDRESS, raw_packet(1), 10.0)

        self.assertEqual(snapshot.state, PIR_TRIGGER_VERIFY)
        self.assertTrue(snapshot.active)

    def test_radar_confirmed_rawdata_moves_to_confirmed_active(self) -> None:
        shadow = ShadowPowerState()

        snapshot = shadow.update_rawdata(ADDRESS, raw_packet(10), 10.0)

        self.assertEqual(snapshot.state, RADAR_CONFIRMED_ACTIVE)
        self.assertTrue(snapshot.active)
        self.assertTrue(snapshot.radar_present)

    def test_radar_present_distance_moves_to_confirmed_active(self) -> None:
        shadow = ShadowPowerState()

        snapshot = shadow.update_alert(
            ADDRESS,
            "RADAR presence active: dist_cm=75",
            10.0,
        )

        self.assertEqual(snapshot.state, RADAR_CONFIRMED_ACTIVE)
        self.assertTrue(snapshot.active)

    def test_inout_enter_report_updates_visibility_and_confirmed_active(self) -> None:
        shadow = ShadowPowerState()
        report = ReportPacket(
            message=(
                "src=INOUT,event=ENTER,signal=enter,code=10,pir=1,"
                "radar=1,dist_cm=75,state=inside_moving,reason=radar_confirmed"
            ),
            fields={
                "src": "INOUT",
                "event": "ENTER",
                "signal": "enter",
                "code": "10",
                "pir": "1",
                "radar": "1",
                "dist_cm": "75",
                "state": "inside_moving",
                "reason": "radar_confirmed",
            },
        )

        snapshot = shadow.update_report(ADDRESS, report, 10.0)

        self.assertEqual(snapshot.state, RADAR_CONFIRMED_ACTIVE)
        self.assertEqual(snapshot.last_inout_event, "ENTER")
        self.assertEqual(snapshot.last_inout_state, "inside_moving")
        self.assertEqual(snapshot.last_inout_code, "10")
        self.assertEqual(snapshot.last_radar_distance_cm, 75.0)

    def test_radar_absence_uses_grace_before_sleep_ready(self) -> None:
        shadow = ShadowPowerState()
        shadow.update_alert(ADDRESS, "RADAR presence active: dist_cm=75", 10.0)

        still_active = shadow.update_alert(
            ADDRESS,
            "RADAR presence inactive: dist_cm=0",
            12.0,
        )
        self.assertEqual(still_active.state, RADAR_CONFIRMED_ACTIVE)

        ready = shadow.update_alert(ADDRESS, "unrelated debug line", 22.1)
        self.assertEqual(ready.state, SLEEP_READY)
        self.assertFalse(ready.active)

    def test_mic_high_has_no_effect_before_presence_confirmation(self) -> None:
        shadow = ShadowPowerState()
        shadow.update_alert(ADDRESS, "MIC activity inactive: rms=10", 1.0)

        snapshot = shadow.update_alert(ADDRESS, "MIC activity active: rms=200", 4.0)

        self.assertEqual(snapshot.state, ABSENT_SLEEP)

    def test_mic_high_sustained_after_radar_confirmation(self) -> None:
        shadow = ShadowPowerState()
        shadow.update_alert(ADDRESS, "RADAR presence active: dist_cm=50", 1.0)
        shadow.update_alert(ADDRESS, "MIC activity inactive: rms=10", 1.1)
        shadow.update_alert(ADDRESS, "MIC activity active: rms=200", 2.0)

        snapshot = shadow.update_alert(ADDRESS, "MIC activity active: rms=220", 4.6)

        self.assertEqual(snapshot.state, MIC_ASSISTED_ACTIVE)

    def test_command_hints_do_not_change_active_sleep_decision(self) -> None:
        shadow = ShadowPowerState()

        enter = shadow.update_command_hint(
            ADDRESS,
            "inout_confirm,bid=a1,cid=1,state=in,rid=1",
            1.0,
        )
        exit_ = shadow.update_command_hint(
            ADDRESS,
            "inout_confirm,bid=a1,cid=2,state=out,rid=2",
            2.0,
        )

        self.assertEqual(enter.state, ABSENT_SLEEP)
        self.assertEqual(exit_.state, ABSENT_SLEEP)
        self.assertEqual(
            exit_.last_command_hint,
            "inout_confirm,bid=a1,cid=2,state=out,rid=2",
        )

    def test_disconnect_marks_disconnected_without_command_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AppPaths.from_base(tmpdir)
            shadow = ShadowPowerState(paths)
            commands: list[CommandEvent] = []

            snapshot = shadow.mark_disconnected(ADDRESS, 10.0)

            self.assertEqual(snapshot.state, DISCONNECTED)
            self.assertEqual(commands, [])
            self.assertTrue((paths.programdata_dir / "power_shadow.log").exists())


if __name__ == "__main__":
    unittest.main()
