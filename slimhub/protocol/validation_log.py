"""DEAN Node v2 LOGDATA payload decoding and validation ACK helpers.

Decoders are ported verbatim from dean_node_v2 ``tools/slimhub_validation.py``
(the Node-side reference, commit 121bfe9). The Node defines this wire format:
VLE1 critical event, VLR1 available range, VLM1 metadata chunk, VLG1 detail
records, VLB1 durable battery records, VLS1 transfer status (never ACKed).
"""
from __future__ import annotations

import math
import re
import struct
import zlib

RECORD_BYTES = 64


CHANNELS = ["temperature_c", "humidity_pct", "iaq", "eco2_ppm", "bvoc_ppm"]


RAW_CHANNELS = ["temperature_c", "humidity_pct", "pressure_hpa", "gas_resistance_kohm"]


def decode_record(data):
    if len(data) != RECORD_BYTES or data[:3] != b"DL\x01":
        raise ValueError("unknown record length/magic/version")
    if zlib.crc32(data[:60]) != struct.unpack_from("<I", data, 60)[0]:
        raise ValueError("record CRC mismatch")
    kind, flags, length = struct.unpack_from("<BHH", data, 3)
    seq, timestamp, session, metadata = struct.unpack_from("<QQII", data, 8)
    result = dict(version=1, type=kind, flags=flags, seq=seq,
                  monotonic_ms=timestamp, session_id=session, metadata_id=metadata)
    if flags & ~(0x3F0F if kind == 4 else 31 if kind in (1, 5) else 15):
        raise ValueError("unknown record flags")
    if kind == 1:
        if length != 28 or data[58:60] != b"\0\0":
            raise ValueError("invalid ENV payload")
        valid, input_valid, encodable, accuracy, status, fallback = data[52:58]
        if (valid | input_valid | encodable) & ~31 or accuracy > 3 or status & ~3 or fallback & ~3:
            raise ValueError("invalid ENV masks/quality")
        values, pairs = [], []
        for channel in range(5):
            integer, fraction = struct.unpack_from("<hh", data, 32 + 4 * channel)
            if not -9999 <= fraction <= 9999 or integer * fraction < 0:
                raise ValueError("noncanonical signed decimal pair")
            pairs.append([integer, fraction])
            values.append(integer + fraction / 10000 if encodable & (1 << channel) else None)
        result.update(channels=CHANNELS, decimal_pairs=pairs, values=values,
                      valid_mask=valid, input_valid_mask=input_valid, encodable_mask=encodable,
                      iaq_accuracy=accuracy, status=status, raw_fallback_mask=fallback)
    elif kind == 2:
        if length != 28 or data[59] != 0:
            raise ValueError("invalid SOUND payload")
        count, source, duration, samples, reason = struct.unpack_from("<BBHHB", data, 52)
        if not 1 <= count <= 20 or source not in (1, 2) or duration > timestamp:
            raise ValueError("invalid SOUND count/source/timestamp")
        if any(data[32 + count:52]) or (source == 2 and any(data[32:52])):
            raise ValueError("unused/gated tensor bytes must be zero")
        raw = list(struct.unpack_from(f"<{count}b", data, 32)) if source == 1 else None
        result.update(class_count=count, source="TFLM_INVOKED" if source == 1 else "RMS_GATED",
                      scores_i8=raw, clip_start_ms=timestamp - duration, clip_end_ms=timestamp,
                      sample_count=samples, gate_reason=reason)
    elif kind == 3:
        meta_kind, index, count, size = data[32:36]
        if not 1 <= meta_kind <= 6 or not 0 <= index < count or not 1 <= size <= 24:
            raise ValueError("invalid metadata chunk")
        if length != 4 + size or any(data[36 + size:60]):
            raise ValueError("invalid metadata length/padding")
        result.update(metadata_kind=meta_kind, chunk_index=index, chunk_count=count,
                      chunk_hex=data[36:36 + size].hex())
    elif kind == 4:
        if length != 28 or data[53] != 0:
            raise ValueError("invalid RAW_ENV payload")
        valid, encodable, status, measurement_index, gas_index = data[48:53]
        if (valid | encodable) & ~15 or status & ~0xB0 or gas_index > 15:
            raise ValueError("invalid RAW_ENV masks/status/index")
        values, pairs = [], []
        for channel in range(4):
            integer, fraction = struct.unpack_from("<hh", data, 32 + 4 * channel)
            if not -9999 <= fraction <= 9999 or integer * fraction < 0:
                raise ValueError("noncanonical RAW_ENV decimal pair")
            pairs.append([integer, fraction])
            values.append(integer + fraction / 10000 if encodable & (1 << channel) else None)
        gas_ohm = struct.unpack_from("<f", data, 56)[0]
        if not math.isfinite(gas_ohm) and (not flags & 1 or encodable & 8):
            raise ValueError("nonfinite raw gas lacks invalid representation flags")
        result.update(channels=RAW_CHANNELS, decimal_pairs=pairs, values=values,
                      valid_mask=valid, encodable_mask=encodable, sensor_status=status,
                      raw_quality_flags=(flags >> 8) & 0x3F,
                      new_data=bool(status & 0x80), gas_valid=bool(status & 0x20),
                      heater_stable=bool(status & 0x10), measurement_index=measurement_index,
                      gas_index=gas_index, measurement_duration_ms=struct.unpack_from("<H", data, 54)[0],
                      raw_gas_resistance_ohm=gas_ohm if math.isfinite(gas_ohm) else None,
                      raw_gas_bits_le=data[56:60].hex())
    elif kind == 5:
        if length != 28 or data[32] not in (1, 2, 3, 4):
            raise ValueError("invalid window provenance")
        source, sample_count, slot_index, valid = data[32:36]
        generation, window, scheduled, start, end, missing = struct.unpack_from("<IIIIII", data, 36)
        result.update(source=source, sample_count=sample_count, slot_index=slot_index,
                      valid_modalities=valid, power_generation=generation, window_id=window,
                      scheduled_ms=scheduled, observed_start_ms=start, observed_end_ms=end,
                      missing_flags=missing)
    else:
        raise ValueError("unknown record type")
    return result


def decode_payload(payload):
    if payload[:4] != b"VLG1":
        return decode_runtime_payload(payload)
    if len(payload) < 80 or len(payload) > 208 or (len(payload) - 16) % 64 or payload[:4] != b"VLG1":
        raise ValueError("invalid LOGDATA payload")
    stream, bundle = struct.unpack_from("<QI", payload, 4)
    if stream == 0 or bundle == 0:
        raise ValueError("zero stream/bundle")
    records = [decode_record(payload[i:i + 64]) for i in range(16, len(payload), 64)]
    if any(record["seq"] == 0 for record in records):
        raise ValueError("unassigned record sequence")
    if any(b["seq"] != a["seq"] + 1 for a, b in zip(records, records[1:])):
        raise ValueError("noncontiguous record sequence")
    return dict(stream_id=stream, bundle_id=bundle, records=records)


VALIDATION_STATUS = {1: "active", 2: "done", 3: "aborted"}


VALIDATION_CLOSE = {0: "none", 1: "complete", 2: "deadline", 3: "ack_timeout",
                    4: "link_faults", 5: "remote_closed", 6: "connect_timeout"}


def decode_status_payload(payload):
    """VLS1: Node transfer state. Not ACKed; tells the Hub to keep receiving
    (active) or that the Node turns RF off (done/aborted, data kept)."""
    if len(payload) != 30:
        raise ValueError("invalid status length")
    stream, state, close, pending, faults, remote, resend, link_class, _reserved, \
        remaining_s, admitted = struct.unpack_from("<QBBBBBBBBHQ", payload, 4)
    if not stream or state not in VALIDATION_STATUS or close not in VALIDATION_CLOSE:
        raise ValueError("invalid status fields")
    return dict(format="VLS1", stream_id=stream, bundle_id=0, state=VALIDATION_STATUS[state],
                close_reason=VALIDATION_CLOSE[close], pending_bundles=pending,
                link_faults=faults, remote_closes=remote, resend_count=resend,
                last_link_class=link_class, window_remaining_s=remaining_s,
                log_admitted_through=admitted)


def decode_runtime_payload(payload):
    if not 16 <= len(payload) <= 256:
        raise ValueError("invalid runtime LOGDATA length")
    magic = payload[:4]
    if magic == b"VLS1":
        return decode_status_payload(payload)
    stream, bundle = struct.unpack_from("<QI", payload, 4)
    if not stream:
        raise ValueError("zero stream")
    result = dict(stream_id=stream, bundle_id=bundle, format=magic.decode("ascii"))
    if magic == b"VLE1":
        if len(payload) < 30 or not bundle or payload[16] not in (1, 2, 4, 8):
            raise ValueError("invalid critical event")
        length = payload[29]
        if length > 220 or len(payload) != 30 + length:
            raise ValueError("invalid event text length")
        session, timestamp = struct.unpack_from("<IQ", payload, 17)
        result.update(kind=payload[16], session_id=session, timestamp_ms=timestamp,
                      text=payload[30:].decode("utf-8"))
    elif magic == b"VLR1":
        if len(payload) != 48 or not bundle:
            raise ValueError("invalid available range")
        first, last, unavailable, dropped = struct.unpack_from("<QQQQ", payload, 16)
        if (first == 0) != (last == 0) or first > last:
            raise ValueError("invalid range endpoints")
        result.update(first_seq=first, last_seq=last, unavailable=unavailable, dropped=dropped)
    elif magic == b"VLM1":
        if len(payload) < 25 or not bundle:
            raise ValueError("invalid metadata")
        metadata, kind, total, offset = struct.unpack_from("<IBHH", payload, 16)
        if not metadata or not 1 <= kind <= 6 or not total or offset + len(payload) - 25 > total:
            raise ValueError("invalid metadata chunk")
        result.update(metadata_id=metadata, kind=kind, total=total, offset=offset, data=payload[25:])
    elif magic == b"VLB1":
        if bundle or len(payload) < 92 or (len(payload) - 16) % 76:
            raise ValueError("invalid battery frame")
        records = []
        for i in range(16, len(payload), 76):
            record = payload[i:i + 76]
            rs, seq = struct.unpack_from("<QQ", record, 8)
            if record[:8] != b"DBAT\x01\x01\x30\0" or rs != stream or not seq or record[24] != 1:
                raise ValueError("invalid battery identity/version")
            if zlib.crc32(record[:72]) != struct.unpack_from("<I", record, 72)[0]:
                raise ValueError("battery CRC mismatch")
            records.append(dict(seq=seq, data=record))
        if any(b["seq"] != a["seq"] + 1 for a, b in zip(records, records[1:])):
            raise ValueError("noncontiguous battery records")
        result["records"] = records
    else:
        raise ValueError("unknown runtime LOGDATA version")
    return result


MAX_LOGDATA_PAYLOAD_LEN = 256
VALIDATION_ACK_PATTERN = re.compile(
    r"^validation_ack,kind=(event|log|battery),stream=[0-9a-f]{16},"
    r"bundle=[0-9]{1,10},through=[0-9]{1,20}$"
)


def ack_target(value: dict) -> tuple[str, int, int] | None:
    """(kind, bundle, through) the Node expects after a durable store, or None.

    VLG1 detail decodes without a ``format`` key; it is identified by ``records``.
    """
    fmt = value.get("format")
    if fmt == "VLE1":
        return ("event", value["bundle_id"], value["bundle_id"])
    if fmt == "VLB1":
        return ("battery", 0, value["records"][-1]["seq"])
    if fmt is None and value.get("records"):
        return ("log", value["bundle_id"], value["records"][-1]["seq"])
    return None


def build_validation_ack_command(kind: str, stream: int, bundle: int, through: int) -> str:
    if kind not in ("event", "log", "battery") or not stream or through < 1:
        raise ValueError("invalid validation ACK")
    if (kind == "battery") != (bundle == 0):
        raise ValueError("invalid validation ACK bundle")
    return f"validation_ack,kind={kind},stream={stream:016x},bundle={bundle},through={through}"
