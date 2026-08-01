from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from slimhub.config import AppPaths
from slimhub.protocol.nus import (
    RawDataPacket,
    ReportPacket,
    normalize_mac,
    validate_command_payload,
)


ABSENT_SLEEP = "ABSENT_SLEEP"
PIR_TRIGGER_VERIFY = "PIR_TRIGGER_VERIFY"
RADAR_CONFIRMED_ACTIVE = "RADAR_CONFIRMED_ACTIVE"
MIC_ASSISTED_ACTIVE = "MIC_ASSISTED_ACTIVE"
SLEEP_READY = "SLEEP_READY"
DISCONNECTED = "DISCONNECTED"

ACTIVE_STATES = {PIR_TRIGGER_VERIFY, RADAR_CONFIRMED_ACTIVE, MIC_ASSISTED_ACTIVE}
PIR_HOLD_SECONDS = 3.0
ABSENCE_GRACE_SECONDS = 10.0
MIC_SUSTAIN_SECONDS = 2.5
LEGACY_PIR_ENTER_SIGNAL = 1
RADAR_CONFIRMED_ENTER_SIGNAL = 10
INOUT_INSIDE_STATES = {"inside_moving", "inside_still", "inside_shadow"}


@dataclass
class ShadowDeviceState:
    address: str
    state: str = ABSENT_SLEEP
    connected: bool = False
    active: bool = False
    last_update: float = 0.0
    last_connected_at: float | None = None
    last_disconnected_at: float | None = None
    last_command_hint: str | None = None
    last_command_hint_at: float | None = None
    pir_active_until: float = 0.0
    radar_present: bool = False
    radar_absent_since: float | None = None
    last_radar_at: float | None = None
    last_radar_distance_cm: float | None = None
    presence_confirmed_since_sleep: bool = False
    mic_baseline: float | None = None
    mic_high_since: float | None = None
    mic_absent_since: float | None = None
    last_mic_at: float | None = None
    last_mic_rms: float | None = None
    last_alert: str | None = None
    last_inout_event: str | None = None
    last_inout_state: str | None = None
    last_inout_signal: str | None = None
    last_inout_code: str | None = None
    last_inout_reason: str | None = None
    last_inout_at: float | None = None


class ShadowPowerState:
    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths
        self._states: dict[str, ShadowDeviceState] = {}

    def update_rawdata(
        self,
        address: str,
        frame: RawDataPacket,
        timestamp: float,
    ) -> ShadowDeviceState:
        state = self._get(address)
        old = state.state
        state.last_update = timestamp
        if self._rawdata_is_radar_confirmed_enter(frame):
            state.pir_active_until = timestamp + PIR_HOLD_SECONDS
            self._apply_radar(state, present=True, distance_cm=None, timestamp=timestamp)
        elif self._rawdata_is_legacy_pir_enter(frame):
            state.pir_active_until = timestamp + PIR_HOLD_SECONDS
            state.presence_confirmed_since_sleep = True
            self._set_state(state, PIR_TRIGGER_VERIFY, timestamp)
        else:
            self._evaluate_idle(state, timestamp)
        self._log_if_changed(state, old, timestamp, "rawdata")
        return state

    def update_alert(
        self,
        address: str,
        text: str,
        timestamp: float,
    ) -> ShadowDeviceState:
        state = self._get(address)
        old = state.state
        state.last_update = timestamp
        state.last_alert = text.rstrip("\n")

        parsed = parse_alert_signals(text)
        if parsed.get("pir_active") is True:
            state.pir_active_until = timestamp + PIR_HOLD_SECONDS
            state.presence_confirmed_since_sleep = True
            self._set_state(state, PIR_TRIGGER_VERIFY, timestamp)
        if "radar_present" in parsed:
            self._apply_radar(
                state,
                present=bool(parsed["radar_present"]),
                distance_cm=parsed.get("dist_cm"),
                timestamp=timestamp,
            )
        if "mic_rms" in parsed:
            self._apply_mic(
                state,
                active=bool(parsed.get("mic_active", True)),
                rms=float(parsed["mic_rms"]),
                timestamp=timestamp,
            )

        self._evaluate_idle(state, timestamp)
        self._log_if_changed(state, old, timestamp, "alert")
        return state

    def update_report(
        self,
        address: str,
        report: ReportPacket,
        timestamp: float,
    ) -> ShadowDeviceState:
        state = self._get(address)
        old = state.state
        state.last_update = timestamp

        if report.fields.get("src", "").upper() != "INOUT":
            return state

        event = report.fields.get("event", "")
        inout_state = report.fields.get("state", "")
        signal = report.fields.get("signal", "")
        code = report.fields.get("code", "")
        state.last_inout_event = event or None
        state.last_inout_state = inout_state or None
        state.last_inout_signal = signal or None
        state.last_inout_code = code or None
        state.last_inout_reason = report.fields.get("reason") or None
        state.last_inout_at = timestamp

        normalized_event = event.upper()
        normalized_state = inout_state.lower()
        normalized_signal = signal.lower()
        distance_cm = report.fields.get("dist_cm")
        radar_present = _bool_or_none(report.fields.get("radar"))
        if radar_present is None:
            distance = _float_or_none(distance_cm)
            if distance is not None:
                radar_present = distance > 0

        pir_active = _bool_or_none(report.fields.get("pir"))
        if pir_active is True:
            state.pir_active_until = timestamp + PIR_HOLD_SECONDS

        if (
            normalized_event == "ENTER"
            or normalized_signal == "enter"
            or code == str(RADAR_CONFIRMED_ENTER_SIGNAL)
            or normalized_state in INOUT_INSIDE_STATES
        ):
            self._apply_radar(
                state,
                present=True,
                distance_cm=distance_cm,
                timestamp=timestamp,
            )
        elif normalized_state == "wait_radar" or pir_active is True:
            self._set_state(state, PIR_TRIGGER_VERIFY, timestamp)
        elif normalized_state == "outside" or radar_present is False:
            self._apply_radar(
                state,
                present=False,
                distance_cm=distance_cm,
                timestamp=timestamp,
            )
            self._evaluate_idle(state, timestamp)
        elif radar_present is True:
            self._apply_radar(
                state,
                present=True,
                distance_cm=distance_cm,
                timestamp=timestamp,
            )

        self._log_if_changed(state, old, timestamp, "report")
        return state

    def update_command_hint(
        self,
        address: str,
        command: str,
        timestamp: float,
    ) -> ShadowDeviceState:
        state = self._get(address)
        old = state.state
        state.last_update = timestamp
        state.last_command_hint = validate_command_payload(command)
        state.last_command_hint_at = timestamp
        self._log_if_changed(state, old, timestamp, "command_hint")
        return state

    def mark_connected(self, address: str, timestamp: float) -> ShadowDeviceState:
        state = self._get(address)
        old = state.state
        state.connected = True
        state.last_connected_at = timestamp
        state.last_update = timestamp
        if state.state == DISCONNECTED:
            self._evaluate_idle(state, timestamp)
        self._log_if_changed(state, old, timestamp, "connected")
        return state

    def mark_disconnected(self, address: str, timestamp: float) -> ShadowDeviceState:
        state = self._get(address)
        old = state.state
        state.connected = False
        state.last_disconnected_at = timestamp
        state.last_update = timestamp
        self._set_state(state, DISCONNECTED, timestamp)
        self._log_if_changed(state, old, timestamp, "disconnected")
        return state

    def snapshot(self, address: str | None = None) -> dict[str, object]:
        if address is not None:
            return asdict(self._get(address))
        return {address: asdict(state) for address, state in sorted(self._states.items())}

    def _get(self, address: str) -> ShadowDeviceState:
        normalized = normalize_mac(address)
        if normalized not in self._states:
            self._states[normalized] = ShadowDeviceState(address=normalized)
        return self._states[normalized]

    def _apply_radar(
        self,
        state: ShadowDeviceState,
        *,
        present: bool,
        distance_cm: object,
        timestamp: float,
    ) -> None:
        distance = _float_or_none(distance_cm)
        present = present and (distance is None or distance > 0)
        state.radar_present = present
        state.last_radar_at = timestamp
        state.last_radar_distance_cm = distance
        if present:
            state.radar_absent_since = None
            state.presence_confirmed_since_sleep = True
            self._set_state(state, RADAR_CONFIRMED_ACTIVE, timestamp)
        elif state.presence_confirmed_since_sleep:
            state.radar_absent_since = timestamp

    def _apply_mic(
        self,
        state: ShadowDeviceState,
        *,
        active: bool,
        rms: float,
        timestamp: float,
    ) -> None:
        state.last_mic_at = timestamp
        state.last_mic_rms = rms
        baseline = state.mic_baseline
        if baseline is None:
            state.mic_baseline = rms
            baseline = rms
        if not active:
            state.mic_baseline = (baseline * 0.9) + (rms * 0.1)
            state.mic_high_since = None
            if state.presence_confirmed_since_sleep:
                state.mic_absent_since = timestamp
            return

        high_threshold = max(100.0, baseline * 2.5)
        if not state.presence_confirmed_since_sleep or rms < high_threshold:
            if rms < high_threshold:
                state.mic_baseline = (baseline * 0.95) + (rms * 0.05)
            state.mic_high_since = None
            return

        if state.mic_high_since is None:
            state.mic_high_since = timestamp
        if timestamp - state.mic_high_since >= MIC_SUSTAIN_SECONDS:
            state.mic_absent_since = None
            self._set_state(state, MIC_ASSISTED_ACTIVE, timestamp)

    def _evaluate_idle(self, state: ShadowDeviceState, timestamp: float) -> None:
        if state.state == DISCONNECTED:
            return
        if timestamp < state.pir_active_until:
            if state.state not in ACTIVE_STATES:
                self._set_state(state, PIR_TRIGGER_VERIFY, timestamp)
            return
        if state.state == MIC_ASSISTED_ACTIVE:
            return
        if state.radar_present:
            self._set_state(state, RADAR_CONFIRMED_ACTIVE, timestamp)
            return
        absence_since = state.radar_absent_since or state.mic_absent_since
        if absence_since is not None and timestamp - absence_since < ABSENCE_GRACE_SECONDS:
            return
        if state.presence_confirmed_since_sleep:
            self._set_state(state, SLEEP_READY, timestamp)
            state.presence_confirmed_since_sleep = False
        else:
            self._set_state(state, ABSENT_SLEEP, timestamp)

    def _set_state(
        self,
        state: ShadowDeviceState,
        new_state: str,
        timestamp: float,
    ) -> None:
        state.state = new_state
        state.active = new_state in ACTIVE_STATES
        state.last_update = timestamp

    def _rawdata_is_legacy_pir_enter(self, packet: RawDataPacket) -> bool:
        return (
            packet.flag_human_presence == 1
            and packet.detected == LEGACY_PIR_ENTER_SIGNAL
        )

    def _rawdata_is_radar_confirmed_enter(self, packet: RawDataPacket) -> bool:
        return (
            packet.flag_human_presence == 1
            and packet.detected == RADAR_CONFIRMED_ENTER_SIGNAL
        )

    def _log_if_changed(
        self,
        state: ShadowDeviceState,
        old_state: str,
        timestamp: float,
        reason: str,
    ) -> None:
        if old_state == state.state and reason != "command_hint":
            return
        if self.paths is None:
            return
        path = self.paths.programdata_dir / "power_shadow.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = asdict(state)
        record["timestamp"] = timestamp
        record["old_state"] = old_state
        record["reason"] = reason
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def parse_alert_signals(text: str) -> dict[str, object]:
    lower = text.lower()
    result: dict[str, object] = {}

    if "pir" in lower:
        active = _parse_active(lower)
        if active is not None:
            result["pir_active"] = active

    if "radar" in lower:
        distance = _extract_number(lower, r"dist(?:ance)?_?cm\s*=\s*([0-9]+(?:\.[0-9]+)?)")
        active = _parse_active(lower)
        if active is not None or distance is not None:
            result["radar_present"] = bool(active) or (distance is not None and distance > 0)
            if distance is not None:
                result["dist_cm"] = distance

    if "mic" in lower:
        rms = _extract_number(lower, r"rms\s*=\s*([0-9]+(?:\.[0-9]+)?)")
        active = _parse_active(lower)
        if rms is not None:
            result["mic_rms"] = rms
            result["mic_active"] = True if active is None else active

    return result


def _parse_active(text: str) -> bool | None:
    if re.search(r"\b(active|present|detected)\s*=\s*true\b", text):
        return True
    if re.search(r"\b(active|present|detected)\s*=\s*false\b", text):
        return False
    if re.search(r"\b(inactive|absent)\b", text):
        return False
    if re.search(r"\b(active|present|detected)\b", text):
        return True
    return None


def _extract_number(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text)
    if match is None:
        return None
    return float(match.group(1))


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "present", "active", "detected", "on"}:
        return True
    if text in {"0", "false", "no", "absent", "inactive", "off", "none"}:
        return False
    return None
