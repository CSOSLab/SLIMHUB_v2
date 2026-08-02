from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from slimhub.events import ReportEvent
from slimhub.protocol.nus import normalize_mac
from slimhub.unitspace.clock import UINT32_WRAP


ENV_IDS = {
    "E0": "temperature",
    "E1": "humidity",
    "E2": "iaq",
    "E3": "eco2",
    "E4": "bvoc",
}
SOUND_CLASSES = {
    0: "background",
    1: "hitting",
    2: "speech_tv",
    3: "air_appliances",
    4: "brushing",
    5: "peeing",
    6: "flushing",
    7: "flushing_end",
    8: "watering_low",
    9: "watering_high",
}
TERMINAL_ADL_EVENTS = {"COMPLETE", "PARTIAL", "NO_MATCH"}
FINAL_ADL_EVENTS = {"POP", *TERMINAL_ADL_EVENTS}
ADL_EVENTS = {"PRE-DETECT", *FINAL_ADL_EVENTS}
_BOOT_ID = re.compile(r"^[0-9A-Fa-f]{8}$")

# The field firmware is expected to emit src=ADL final reports. Some deployed
# DEAN Node v2 images only emit the underlying EVENT stream, however. These
# deliberately conservative signatures preserve the small set of strong,
# location-specific matches observed in the legacy debugstr history. They are
# display fallbacks, not firmware ground truth.
DERIVED_ADL_RULES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "BEDROOM": (("watchTV", ("S2",)),),
    "TOILET": (
        ("toothbrush", ("S4", "E1")),
        ("toothbrush", ("S4",)),
        ("pee", ("S5",)),
        ("handwash", ("S9",)),
        ("handwash", ("S8",)),
    ),
    "KITCHEN": (
        ("dishwashing", ("S9",)),
        ("dishwashing", ("S8",)),
        ("makeMeal", ("E1",)),
    ),
}


@dataclass(frozen=True)
class MultimodalRecord:
    kind: str
    mac: str
    timestamp: float
    data: dict[str, object]


class DeploymentManifestStore:
    """Read the optional per-node deployment manifest without mutating it."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def for_node(self, mac: str) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        nodes = document.get("nodes", document)
        if not isinstance(nodes, dict):
            return {}
        value = nodes.get(normalize_mac(mac), {})
        return dict(value) if isinstance(value, dict) else {}


class MultimodalReportStore:
    """Typed, replay-safe storage for firmware-extracted EVENT/ADL reports.

    This store intentionally never returns movement commands. Firmware has
    normally performed the multimodal match. Central records and groups it for
    diagnostics/offline analysis and emits a clearly marked, non-ground-truth
    display fallback for known signatures when a deployed image omits ADL.
    """

    def __init__(self, manifests: DeploymentManifestStore) -> None:
        self.manifests = manifests
        self._seen_analysis: set[tuple[str, str, int]] = set()
        self._seen_sound_runs: set[tuple[str, str, int, int, int]] = set()
        self._baselines: dict[tuple[str, str, str], dict[str, object]] = {}
        self._sessions: dict[tuple[str, str, int], dict[str, object]] = {}
        self._open_session: dict[tuple[str, str], tuple[str, str, int] | None] = {}
        self._pending_d0: dict[tuple[str, str], dict[str, object]] = {}
        self._pending_d1: dict[tuple[str, str], dict[str, object]] = {}
        self._schema2_nodes: set[tuple[str, str]] = set()
        self._activities: dict[tuple[str, str, int], dict[str, object]] = {}
        self._legacy_events: dict[tuple[str, str, int, str], dict[str, object]] = {}
        self._records: list[MultimodalRecord] = []

    def handle(self, event: ReportEvent) -> None:
        fields = event.packet.fields
        src = fields.get("src", "").strip().upper()
        name = fields.get("event", "").strip().upper()
        if src == "EVENT" and name == "BASELINE":
            self._handle_baseline(event)
        elif src == "EVENT" and name in {"ENV", "SOUND"}:
            self._handle_feature(event, name)
        elif src == "ADL" and _normalize_adl_status(name) in ADL_EVENTS:
            self._handle_adl(event, _normalize_adl_status(name))
        elif src in {"EVENT", "ADL"}:
            self._record(event, "multimodal_error", error="unknown_event", source=src, event=name)

    def handle_legacy_json(self, event: ReportEvent) -> None:
        packet = event.packet
        document = packet.document
        if packet.parse_error is not None or not isinstance(document, dict):
            self._record(
                event,
                "legacy_json_invalid",
                error=packet.parse_error or "missing_json_document",
                raw_json=packet.message,
            )
            return
        record_type = str(document.get("type") or "").strip().upper()
        if record_type == "EVENT":
            self._handle_legacy_event(event, document)
        elif record_type == "INFERENCE":
            self._handle_legacy_inference(event, document)
        else:
            self._record(
                event,
                "legacy_json_unknown",
                error="unknown_json_type",
                raw_document=dict(document),
                unknown_keys=sorted(str(key) for key in document),
            )

    def handle_inout(self, event: ReportEvent) -> None:
        fields = event.packet.fields
        if fields.get("src", "").strip().upper() != "INOUT":
            return
        if fields.get("event", "").strip().upper() != "SEQUENCE":
            return
        result = fields.get("result", "").strip().upper()
        event_id = (fields.get("event_id") or fields.get("id") or "").strip().upper()
        mac, boot_id, event_ts = self._identity(event)
        if mac is None or boot_id is None:
            return
        if result == "ENTER_CONFIRMED" and event_id == "D0":
            session_seq = _integer(fields.get("session_seq"))
            key = (mac, boot_id, session_seq) if session_seq is not None else None
            self._open_session[(mac, boot_id)] = key
            boundary = self._boundary(event, "D0")
            if key is not None:
                self._session(key)["d0"] = boundary
            else:
                self._pending_d0[(mac, boot_id)] = boundary
            self._record(
                event,
                "session_boundary",
                boundary="D0",
                session_seq=session_seq,
                inout_event_seq=_integer(fields.get("event_seq")),
                event_ts_ms=event_ts,
            )
        elif result in {"EXIT_CONFIRMED", "EXIT_SYNC"} and event_id == "D1":
            key = self._open_session.pop((mac, boot_id), None)
            boundary = self._boundary(event, "D1")
            if key is not None:
                session = self._session(key)
                session["d1"] = boundary
            else:
                self._pending_d1[(mac, boot_id)] = boundary
            self._record(
                event,
                "session_boundary",
                boundary="D1",
                session_seq=key[2] if key is not None else None,
                inout_event_seq=_integer(fields.get("event_seq")),
                event_ts_ms=event_ts,
            )
            if key is not None:
                self._record_derived_inference(event, key, session)

    def drain_records(self) -> list[MultimodalRecord]:
        records, self._records = self._records, []
        return records

    def snapshot(self) -> dict[str, object]:
        return {
            "baselines": {"/".join(key): value for key, value in self._baselines.items()},
            "sessions": {"/".join(map(str, key)): value for key, value in self._sessions.items()},
            "activities": {
                "/".join(map(str, key)): value for key, value in self._activities.items()
            },
            "legacy_events": {
                "/".join(map(str, key)): value for key, value in self._legacy_events.items()
            },
        }

    def _handle_legacy_event(
        self,
        event: ReportEvent,
        document: dict[str, object],
    ) -> None:
        action = str(document.get("event") or "").strip().upper()
        value = _integer(document.get("value"))
        boot_id = _text(document.get("bid"))
        event_id = _integer(document.get("eid"))
        event_ts = _uint32(document.get("ts"))
        schema = _integer(document.get("schema"))
        errors = _legacy_common_errors(event, document, boot_id, schema)
        if action not in {"ENTER", "EXIT"}:
            errors.append("invalid_event")
        expected_value = 10 if action == "ENTER" else 20 if action == "EXIT" else None
        if expected_value is None or value != expected_value:
            errors.append("invalid_event_value")
        if event_id is None:
            errors.append("invalid_eid")
        if event_ts is None:
            errors.append("invalid_ts")
        data: dict[str, object] = {
            "source": "LEGACY_JSON",
            "event": action,
            "value": value,
            "schema": schema,
            "boot_id": boot_id,
            "event_id": event_id,
            "event_ts_ms": event_ts,
            "raw_document": dict(document),
            "identity_warning": event.identity_warning,
            "errors": errors,
            "timeline_eligible": not any(
                error in {"invalid_event", "invalid_event_value", "invalid_bid", "invalid_eid", "invalid_ts"}
                for error in errors
            ),
        }
        if not data["timeline_eligible"]:
            self._record(event, "legacy_event_invalid", **data)
            return
        key = (normalize_mac(event.mac), boot_id or "", event_ts or 0, action)
        if key in self._legacy_events:
            self._record(event, "legacy_event_replay", **data)
            return
        data["timeline_key"] = "/".join(map(str, key))
        self._legacy_events[key] = data
        self._record(event, "legacy_event", **data)

    def _handle_legacy_inference(
        self,
        event: ReportEvent,
        document: dict[str, object],
    ) -> None:
        status = _normalize_adl_status(document.get("status"))
        boot_id = _text(document.get("bid"))
        session_seq = _integer(document.get("sid"))
        analysis_seq = _integer(document.get("aid"))
        schema = _integer(document.get("schema"))
        truth = _number(document.get("truth"))
        sequence = str(document.get("sequence") or "")
        why = str(document.get("why") or "")
        missing_value = document.get("missing")
        missing = "" if missing_value is None else str(missing_value)
        errors = _legacy_common_errors(event, document, boot_id, schema)
        if status not in ADL_EVENTS:
            errors.append("invalid_inference_status")
        if session_seq is None:
            errors.append("invalid_sid")
        if analysis_seq is None:
            errors.append("invalid_aid")
        if truth is None or (schema == 2 and not 0 <= truth <= 1):
            errors.append("invalid_truth")
        if len(sequence) > 31:
            errors.append("sequence_contract_violation")
        if why not in {
            "threshold_pop",
            "d1_complete",
            "d1_partial",
            "d1_no_match",
            "history_overflow",
            "new_session",
        }:
            errors.append("unknown_why")
        data: dict[str, object] = {
            "source": "LEGACY_JSON",
            "event": status,
            "schema": schema,
            "boot_id": boot_id,
            "session_seq": session_seq,
            "analysis_seq": analysis_seq,
            "adl": document.get("ADL"),
            "truth": truth,
            "truth_semantics": "adaptive_score_ratio" if schema == 2 else "legacy_heap_truth",
            "missing": missing,
            "sequence": sequence,
            "why": why,
            "provisional": status == "PRE-DETECT",
            "final": status in FINAL_ADL_EVENTS,
            "ground_truth_eligible": status in FINAL_ADL_EVENTS,
            "raw_document": dict(document),
            "identity_warning": event.identity_warning,
            "errors": errors,
        }
        valid_boot_id = boot_id is not None and _BOOT_ID.fullmatch(boot_id) is not None
        valid_truth = truth is not None and (schema != 2 or 0 <= truth <= 1)
        if not valid_boot_id and "invalid_bid" not in errors:
            errors.append("invalid_bid")
        if (
            not valid_boot_id
            or not valid_truth
            or analysis_seq is None
            or session_seq is None
            or status not in ADL_EVENTS
        ):
            self._record(event, "legacy_inference_invalid", **data)
            return
        assert boot_id is not None
        key = (normalize_mac(event.mac), boot_id, analysis_seq)
        if schema == 2:
            self._schema2_nodes.add((key[0], boot_id))
        activity, replay = self._upsert_activity(key, data, source="legacy_json")
        if replay:
            self._record(event, "legacy_activity_replay", **data)
            return
        data["activity_key"] = activity["activity_key"]
        self._link_activity_to_session(key[0], boot_id, session_seq, activity)
        session = self._session((key[0], boot_id, session_seq))
        if status in TERMINAL_ADL_EVENTS:
            session["legacy_final"] = data
        elif status == "POP":
            session.setdefault("legacy_pops", []).append(data)
        elif status == "PRE-DETECT":
            session["legacy_predetect"] = data
        self._record(event, "legacy_activity", **data)

    def _upsert_activity(
        self,
        key: tuple[str, str, int],
        data: dict[str, object],
        *,
        source: str,
    ) -> tuple[dict[str, object], bool]:
        activity = self._activities.setdefault(
            key,
            {
                "activity_key": "/".join(map(str, key)),
                "mac": key[0],
                "boot_id": key[1],
                "analysis_seq": key[2],
                "sources": [],
            },
        )
        replay = source in activity["sources"]
        if replay:
            return activity, True
        activity["sources"].append(source)
        activity[source] = dict(data)
        # Typed ADL contains score/coverage/margin/reset diagnostics and is the
        # canonical detail whenever both migration reports exist.
        activity["canonical_source"] = "typed_adl" if "typed_adl" in activity else source
        activity["canonical_detail"] = dict(activity[activity["canonical_source"]])
        return activity, False

    def _link_activity_to_session(
        self,
        mac: str,
        boot_id: str,
        session_seq: int,
        activity: dict[str, object],
    ) -> None:
        session = self._session((mac, boot_id, session_seq))
        states = session.setdefault("activity_states", [])
        key = activity["activity_key"]
        if key not in states:
            states.append(key)

    def _handle_baseline(self, event: ReportEvent) -> None:
        mac, boot_id, event_ts = self._identity(event)
        if mac is None or boot_id is None:
            return
        fields = event.packet.fields
        ready_mask = (fields.get("ready_mask") or fields.get("base") or "").lower()
        required_mask = (fields.get("required_mask") or fields.get("required") or "").lower()
        counts = _counts(fields.get("counts"))
        schema = _integer(fields.get("schema"))
        errors = _schema_errors(schema, event_ts)
        if schema == 2:
            self._schema2_nodes.add((mac, boot_id))
        if ready_mask == "" or required_mask == "" or counts is None:
            errors.append("malformed_baseline")
        manifest = self.manifests.for_node(mac)
        configured_profile = fields.get("configured_profile") or fields.get("profile", "")
        profile_error = _profile_error(event.location, configured_profile, manifest)
        if profile_error:
            errors.append(profile_error)
        data: dict[str, object] = {
            "source": "EVENT",
            "event": "BASELINE",
            "schema": schema,
            "boot_id": boot_id,
            "event_ts_ms": event_ts,
            "configured_profile": configured_profile,
            "status": fields.get("status"),
            "ready_mask": ready_mask,
            "required_mask": required_mask,
            "samples": _integer(fields.get("samples")),
            "counts": counts,
            "channel_order": ["E0", "E1", "E2", "E3", "E4"],
            "manifest": manifest,
            "errors": errors,
        }
        key = (mac, boot_id, ready_mask)
        previous = self._baselines.get(key)
        if previous is not None and not _baseline_is_newer(previous, data):
            self._record(event, "baseline_replay", **data)
            return
        self._baselines[key] = data
        self._record(event, "baseline", **data)

    def _handle_feature(self, event: ReportEvent, name: str) -> None:
        mac, boot_id, event_ts = self._identity(event)
        if mac is None or boot_id is None:
            return
        fields = event.packet.fields
        analysis_seq = _integer(fields.get("analysis_seq") or fields.get("aid"))
        session_seq = _integer(fields.get("session_seq") or fields.get("sid"))
        if name == "SOUND" and analysis_seq is None:
            sound_class = _integer(fields.get("class_index") or fields.get("class"))
            if session_seq is None or sound_class is None or event_ts is None:
                self._record(
                    event,
                    "multimodal_error",
                    error="missing_or_malformed_sound_identity",
                )
                return
            sound_key = (mac, boot_id, session_seq, event_ts, sound_class)
            if sound_key in self._seen_sound_runs:
                self._record(
                    event,
                    "multimodal_replay",
                    session_seq=session_seq,
                    event_ts_ms=event_ts,
                    class_index=sound_class,
                )
                return
            self._seen_sound_runs.add(sound_key)
        elif not self._claim_analysis(event, mac, boot_id, analysis_seq):
            return
        schema = _integer(fields.get("schema"))
        errors = _schema_errors(schema, event_ts)
        if schema == 2:
            self._schema2_nodes.add((mac, boot_id))
        event_id = (fields.get("event_id") or "").upper()
        data: dict[str, object] = {
            "source": "EVENT",
            "event": name,
            "schema": schema,
            "boot_id": boot_id,
            "session_seq": session_seq,
            "analysis_seq": analysis_seq,
            "event_ts_ms": event_ts,
            "event_id": event_id,
            "start_ms": _integer(fields.get("start_ms")),
            "duration_ms": _integer(fields.get("duration_ms")),
            "confidence": _integer(fields.get("confidence")),
            "event_order": _event_order(event, event_ts, analysis_seq),
            "raw_fields": dict(fields),
            "errors": errors,
        }
        if name == "ENV":
            data.update(
                canonical_name=ENV_IDS.get(event_id),
                baseline=_number(fields.get("baseline")),
                peak=_number(fields.get("peak")),
                delta_levels=_integer(fields.get("delta_levels")),
            )
            if event_id not in ENV_IDS:
                errors.append("unknown_env_id")
        else:
            from slimhub.logging.sound_schema import resolve_node_sound_schema

            class_count = _integer(fields.get("class_count"))
            sound_class = _integer(fields.get("class_index") or fields.get("class"))
            semantic_value = fields.get("semantic")
            profile = fields.get("profile")
            location = fields.get("location") or event.location
            model = fields.get("model")
            sound_schema = resolve_node_sound_schema(
                profile=profile,
                location=location,
                class_count=class_count,
                semantic=semantic_value,
            )
            explicit_semantic = str(semantic_value or "").strip()
            if explicit_semantic.lower() in {
                "",
                "0",
                "1",
                "false",
                "true",
                "ready",
                "disabled",
            }:
                explicit_semantic = ""
            label = (
                explicit_semantic
                or (
                    sound_schema.labels[sound_class]
                    if sound_schema is not None
                    and sound_class is not None
                    and 0 <= sound_class < sound_schema.class_count
                    else None
                )
                or (
                    SOUND_CLASSES.get(sound_class)
                    if schema == 1 and sound_class is not None
                    else None
                )
            )
            data.update(
                class_count=class_count,
                class_index=sound_class,
                **{"class": sound_class},
                label=label,
                semantic=semantic_value,
                profile=profile,
                location=location,
                model=model,
                count=_integer(fields.get("count")),
                max=_number(fields.get("max")),
                mean=_number(fields.get("mean")),
            )
            if class_count is None or not 1 <= class_count <= 16:
                errors.append("sound_class_count_out_of_range")
            if schema == 1 and class_count != 10:
                errors.append("sound_class_count_must_be_10")
            if sound_class is None or class_count is None or not 0 <= sound_class < class_count:
                errors.append("unknown_sound_class")
            elif event_id and event_id != f"S{sound_class}":
                errors.append("sound_id_class_mismatch")
            if label is None and schema == 2:
                errors.append("semantic_unavailable")
            if sound_class == 0:
                errors.append("background_is_not_positive_adl_event")
        self._attach_to_session(mac, boot_id, session_seq, data)
        self._record(event, "feature", **data)

    def _handle_adl(self, event: ReportEvent, name: str) -> None:
        mac, boot_id, event_ts = self._identity(event)
        if mac is None or boot_id is None:
            return
        fields = event.packet.fields
        analysis_seq = _integer(fields.get("analysis_seq") or fields.get("aid"))
        if not self._claim_analysis(event, mac, boot_id, analysis_seq):
            return
        session_seq = _integer(fields.get("session_seq") or fields.get("sid"))
        schema = _integer(fields.get("schema"))
        errors = _schema_errors(schema, event_ts)
        if schema == 2:
            self._schema2_nodes.add((mac, boot_id))
        why = fields.get("why")
        overflow = _integer(fields.get("overflow"))
        if overflow is None and why == "history_overflow":
            overflow = 1
        stage_text = fields.get("stages") or fields.get("m")
        stages = _stages(stage_text)
        missing = _integer(fields.get("missing"))
        if missing is None and stages is not None:
            missing = max(stages["total"] - stages["matched"], 0)
        data: dict[str, object] = {
            "source": "ADL",
            "event": name,
            "schema": schema,
            "boot_id": boot_id,
            "session_seq": session_seq,
            "analysis_seq": analysis_seq,
            "event_ts_ms": event_ts,
            "profile": fields.get("profile") or fields.get("room"),
            "room": fields.get("room"),
            "adl": fields.get("adl"),
            "truth": _number(fields.get("truth") or fields.get("score")),
            "truth_semantics": "adaptive_score_percent" if schema == 2 else "legacy_heap_truth",
            "score": _number(fields.get("score")),
            "coverage": _number(fields.get("coverage") or fields.get("cov")),
            "margin": _number(fields.get("margin")),
            "progress": _integer(fields.get("progress")),
            "stages": stages,
            "missing": missing,
            "duration_ms": _integer(fields.get("duration_ms") or fields.get("dur")),
            "overflow": overflow,
            "sequence": fields.get("sequence") or fields.get("seq", ""),
            "why": why,
            "activity_count": _integer(fields.get("act")),
            "pop_count": _integer(fields.get("pop")),
            "environment_count": _integer(fields.get("env")),
            "source_count": _integer(_last_duplicate(event.packet, "src")),
            "pop_reset": (
                fields.get("pop_reset") or fields.get("reset") or fields.get("rst")
            ),
            "event_order": _event_order(event, event_ts, analysis_seq),
            "provisional": name == "PRE-DETECT",
            "final": name in FINAL_ADL_EVENTS,
            "ground_truth_eligible": name in TERMINAL_ADL_EVENTS and overflow != 1,
            "errors": errors,
        }
        sequence_limit = 31 if schema == 2 else 16
        if len(str(data["sequence"])) > sequence_limit:
            errors.append("sequence_contract_violation")
        # profile describes an AUTO build's winning candidate and must not be
        # mistaken for the image configuration; BASELINE/manifest are canonical.
        manifest = self.manifests.for_node(mac)
        if manifest:
            data["manifest"] = manifest
        self._attach_to_session(mac, boot_id, session_seq, data)
        if analysis_seq is not None:
            key = (mac, boot_id, analysis_seq)
            activity, _ = self._upsert_activity(key, data, source="typed_adl")
            data["activity_key"] = activity["activity_key"]
            if session_seq is not None:
                self._link_activity_to_session(mac, boot_id, session_seq, activity)
        self._record(event, "adl_result", **data)

    def _identity(self, event: ReportEvent) -> tuple[str | None, str | None, int | None]:
        fields = event.packet.fields
        mac = normalize_mac(event.mac)
        boot_id = _text(fields.get("boot_id") or fields.get("bid"))
        event_ts = _uint32(fields.get("event_ts_ms") or fields.get("ts"))
        if boot_id is None or event_ts is None:
            self._record(event, "multimodal_error", error="missing_or_malformed_identity")
            return mac, None, None
        return mac, boot_id, event_ts

    def _claim_analysis(
        self,
        event: ReportEvent,
        mac: str,
        boot_id: str,
        analysis_seq: int | None,
    ) -> bool:
        if analysis_seq is None:
            self._record(event, "multimodal_error", error="missing_or_malformed_analysis_seq")
            return False
        key = (mac, boot_id, analysis_seq)
        if key in self._seen_analysis:
            self._record(event, "multimodal_replay", analysis_seq=analysis_seq)
            return False
        self._seen_analysis.add(key)
        return True

    def _attach_to_session(
        self,
        mac: str,
        boot_id: str,
        session_seq: int | None,
        data: dict[str, object],
    ) -> None:
        if session_seq is None:
            data["session_link"] = "missing_session_seq"
            return
        key = (mac, boot_id, session_seq)
        session = self._session(key)
        session.setdefault("records", []).append(data)
        session["records"].sort(key=lambda record: tuple(record.get("event_order") or (0, 0)))
        if data.get("event") in TERMINAL_ADL_EVENTS:
            session["final"] = data
        elif data.get("event") == "POP":
            session.setdefault("pops", []).append(data)
        elif data.get("event") == "PRE-DETECT":
            session["predetect"] = data
        node_key = (mac, boot_id)
        if self._open_session.get(node_key) is None:
            self._open_session[(mac, boot_id)] = key
            pending_d0 = self._pending_d0.pop(node_key, None)
            if pending_d0 is not None:
                session["d0"] = pending_d0
            pending_d1 = self._pending_d1.pop(node_key, None)
            if pending_d1 is not None:
                session["d1"] = pending_d1
        data["session_link"] = "linked"

    def _session(self, key: tuple[str, str, int]) -> dict[str, object]:
        return self._sessions.setdefault(
            key,
            {"mac": key[0], "boot_id": key[1], "session_seq": key[2], "records": []},
        )

    def _boundary(self, event: ReportEvent, name: str) -> dict[str, object]:
        return {
            "event_id": name,
            "event_ts_ms": _uint32(
                event.packet.fields.get("event_ts_ms") or event.packet.fields.get("ts")
            ),
            "normalized_timestamp": event.normalized_timestamp,
            "inout_event_seq": _integer(event.packet.fields.get("event_seq")),
        }

    def _record_derived_inference(
        self,
        event: ReportEvent,
        key: tuple[str, str, int],
        session: dict[str, object],
    ) -> None:
        # Prefer a real firmware final and never create the fallback twice for
        # a replayed D1 report.
        if (
            session.get("final") is not None
            or session.get("legacy_final") is not None
            or session.get("derived_final") is not None
        ):
            return
        if (key[0], key[1]) in self._schema2_nodes:
            return
        result = _derive_adl(event.location, session.get("records"))
        if result is None:
            return
        data: dict[str, object] = {
            "source": "CENTRAL_DERIVED",
            "event": "COMPLETE",
            "boot_id": key[1],
            "session_seq": key[2],
            "event_ts_ms": _uint32(event.packet.fields.get("event_ts_ms")),
            "adl": result["adl"],
            "truth": result["truth"],
            "missing": "",
            "sequence": result["sequence"],
            "final": True,
            "ground_truth_eligible": False,
            "derived": True,
            "derived_from": "legacy_location_event_signature",
        }
        session["derived_final"] = data
        self._record(event, "derived_inference", **data)

    def _record(self, report_event: ReportEvent, kind: str, **data: object) -> None:
        context = {
            "ble_address": report_event.source_address,
            "location": report_event.location,
            "device_type": report_event.device_type,
            "session_id": report_event.session_id,
            "connected": report_event.connected,
            "packet_type": "REPORT",
            "raw_payload_hex": report_event.payload.hex(),
            "receipt_ts": report_event.receipt_timestamp or report_event.timestamp,
            "clock_offset_ms": report_event.clock_offset_ms,
            "clock_error_ms": report_event.clock_error_ms,
            "wrap_epoch": report_event.wrap_epoch,
            "parsed_fields": dict(report_event.packet.fields),
        }
        context.update(data)
        self._records.append(
            MultimodalRecord(
                kind=kind,
                mac=normalize_mac(report_event.mac),
                timestamp=report_event.receipt_timestamp or report_event.timestamp,
                data=context,
            )
        )


def _derive_adl(location: str, records: object) -> dict[str, object] | None:
    if not isinstance(records, list):
        return None
    evidence: dict[str, float] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("source") != "EVENT":
            continue
        event_id = str(record.get("event_id") or "").upper()
        if not event_id or _has_schema_error(record.get("errors")):
            continue
        confidence = _number(record.get("confidence"))
        if confidence is None:
            continue
        evidence[event_id] = max(evidence.get(event_id, 0.0), confidence)

    for adl, signature in DERIVED_ADL_RULES.get(location.upper(), ()):
        if all(event_id in evidence for event_id in signature):
            truth = min(evidence[event_id] for event_id in signature)
            if truth > 1:
                truth /= 100
            return {
                "adl": adl,
                "truth": round(truth, 2),
                "sequence": f"D0_{'_'.join(signature)}_D1_",
            }
    return None


def _has_schema_error(errors: object) -> bool:
    if not isinstance(errors, list):
        return False
    return any("schema" in str(error) or "unknown_" in str(error) for error in errors)


def _event_order(event: ReportEvent, event_ts: int | None, analysis_seq: int | None) -> tuple[int, int]:
    uptime = (event.wrap_epoch or 0) * UINT32_WRAP + (event_ts or 0)
    return uptime, analysis_seq or 0


def _schema_errors(schema: int | None, event_ts: int | None) -> list[str]:
    errors = []
    if schema not in {1, 2}:
        errors.append("unknown_schema")
    if event_ts is None:
        errors.append("malformed_event_ts_ms")
    return errors


def _normalize_adl_status(value: object) -> str:
    status = str(value or "").strip().upper().replace("_", "-")
    if status == "PREDETECT":
        return "PRE-DETECT"
    return status


def _legacy_common_errors(
    event: ReportEvent,
    document: dict[str, object],
    boot_id: str | None,
    schema: int | None,
) -> list[str]:
    errors: list[str] = []
    if event.identity_warning:
        errors.append(event.identity_warning)
    if boot_id is None or not _BOOT_ID.fullmatch(boot_id):
        errors.append("invalid_bid")
    if schema != 2:
        errors.append("future_or_legacy_schema")
    if "device" not in document:
        errors.append("missing_device")
    return errors


def _profile_error(location: str, configured_profile: str, manifest: dict[str, object]) -> str | None:
    profile = configured_profile.strip().lower()
    expected = str(manifest.get("configured_profile") or "").strip().lower()
    location_key = location.strip().lower()
    if expected and profile and expected != profile:
        return "manifest_profile_mismatch"
    if profile in {"toilet", "kitchen"} and location_key not in {profile, "undefined", ""}:
        return "fixed_profile_location_mismatch"
    return None


def _baseline_is_newer(previous: dict[str, object], current: dict[str, object]) -> bool:
    previous_counts = sum(value or 0 for value in (previous.get("counts") or []))
    current_counts = sum(value or 0 for value in (current.get("counts") or []))
    return (current.get("event_ts_ms") or 0, current_counts) >= (
        previous.get("event_ts_ms") or 0,
        previous_counts,
    )


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _integer(value: object) -> int | None:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return None


def _uint32(value: object) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and 0 <= parsed < UINT32_WRAP else None


def _number(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _counts(value: object) -> list[int] | None:
    parts = str(value).split("/")
    if len(parts) != 5:
        return None
    parsed = [_integer(part) for part in parts]
    return parsed if all(part is not None and part >= 0 for part in parsed) else None


def _stages(value: object) -> dict[str, int] | None:
    parts = str(value).split("/")
    if len(parts) != 2:
        return None
    matched, total = (_integer(part) for part in parts)
    if matched is None or total is None:
        return None
    return {"matched": matched, "total": total}


def _last_duplicate(packet: object, key: str) -> object:
    duplicates = getattr(packet, "duplicate_fields", None)
    if not isinstance(duplicates, dict):
        return None
    values = duplicates.get(key)
    return values[-1] if isinstance(values, list) and values else None
