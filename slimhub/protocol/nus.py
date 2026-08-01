from __future__ import annotations

import json
import logging
import re
import string
import struct
from dataclasses import dataclass


NUS_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_RX_WRITE_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX_NOTIFY_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

DEFAULT_DEVICE_NAME = "DEAN_NODE_V2"
VALID_COMMANDS: tuple[str, ...] = ()
RECORD_COMMAND = "record"
RECORD_STOP_COMMAND = "record_stop"
SOUND_STOP_COMMAND = "sound_stop"
SOUND_STATUS_COMMAND = "sound_status"
MIN_RECORD_SECONDS = 1
MAX_RECORD_SECONDS = 300
MIN_SOUND_LABEL_LEN = 1
MAX_SOUND_LABEL_LEN = 24
MAX_SOUND_THRESHOLD_RMS = 32767
MIN_SOUND_SECONDS = 1
MAX_SOUND_SECONDS = 1800
MAX_SOUND_SILENCE_SECONDS = 60
SOUND_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
COMMAND_ALIASES: dict[str, str] = {}
END_FLAG = b"\x0d\x0a"
MAC_LEN = 6
PACKET_TYPE_LEN = 8
PACKET_LEN_LEN = 2
HEADER_LEN = MAC_LEN + PACKET_TYPE_LEN + PACKET_LEN_LEN
END_FLAG_LEN = len(END_FLAG)
RAWDATA_PAYLOAD_LEN = 33
MIN_COMMAND_PAYLOAD_LEN = 1
MAX_COMMAND_PAYLOAD_LEN = 128
MAX_REPORT_PAYLOAD_LEN = 256
MAX_FRAME_LEN = 4096
KNOWN_PACKET_TYPES = {"RAWDATA", "ALERT", "REPORT", "AUDIO", "WAVFILE"}
NODE_SIMPLE_COMMANDS = {"node_status", "config_get", "config_reload"}


class PacketParseError(ValueError):
    """Raised when a binary packet cannot be parsed."""


@dataclass(frozen=True)
class RawDataPacket:
    flag_human_presence: int
    detected: int
    flag_env: int
    temperature_c: float
    humidity: int
    iaq: int
    eco2: int
    bvoc: int
    accuracy: int
    flag_sound: int
    sound: list[int]
    is_pir_human_detection_event: bool


@dataclass(frozen=True)
class AlertPacket:
    message: str


@dataclass(frozen=True)
class ReportPacket:
    message: str
    fields: dict[str, str]
    format: str = "csv"
    document: dict[str, object] | None = None
    parse_error: str | None = None
    duplicate_fields: dict[str, list[str]] | None = None


@dataclass(frozen=True)
class IgnoredPacket:
    """Migration-only packet that must never start Central file storage."""

    packet_type: str
    payload_bytes: int


@dataclass(frozen=True)
class ParsedFrame:
    mac: str
    mac_bytes: bytes
    packet_type: str
    packet_length: int
    payload: bytes
    parsed: RawDataPacket | AlertPacket | ReportPacket | IgnoredPacket


class FrameAssembler:
    """Accumulates BLE notify chunks until a complete length-delimited frame exists."""

    def __init__(self, max_frame_len: int = MAX_FRAME_LEN) -> None:
        self._buffer = bytearray()
        self._max_frame_len = max_frame_len
        self._max_buffer_len = max_frame_len * 2

    def push(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        frames: list[bytes] = []

        if len(self._buffer) > self._max_buffer_len:
            dropped = len(self._buffer) - self._max_frame_len
            del self._buffer[:dropped]
            logging.warning("Dropping %d bytes while bounding NUS reassembly buffer", dropped)

        while len(self._buffer) >= HEADER_LEN:
            raw_packet_type = bytes(
                self._buffer[MAC_LEN : MAC_LEN + PACKET_TYPE_LEN]
            )
            try:
                packet_type = raw_packet_type.rstrip(b"\x00").decode("ascii")
            except UnicodeDecodeError:
                packet_type = ""
            if packet_type not in KNOWN_PACKET_TYPES:
                bad_byte = self._buffer.pop(0)
                logging.warning(
                    "Dropping byte 0x%02x while resynchronizing: invalid packet type",
                    bad_byte,
                )
                continue
            packet_length = int.from_bytes(
                self._buffer[MAC_LEN + PACKET_TYPE_LEN : HEADER_LEN],
                byteorder="little",
                signed=False,
            )
            if (
                (packet_type == "RAWDATA" and packet_length != RAWDATA_PAYLOAD_LEN)
                or (packet_type == "REPORT" and packet_length > MAX_REPORT_PAYLOAD_LEN)
            ):
                mac = ":".join(f"{byte:02X}" for byte in self._buffer[:MAC_LEN])
                bad_byte = self._buffer.pop(0)
                logging.warning(
                    "NUS reject mac=%s frame_type=%s declared_length=%d "
                    "actual_available=%d reason=invalid_payload_length "
                    "resync_byte=0x%02x",
                    mac,
                    packet_type,
                    packet_length,
                    max(0, len(self._buffer) - HEADER_LEN),
                    bad_byte,
                )
                continue
            frame_len = HEADER_LEN + packet_length + END_FLAG_LEN

            if frame_len > self._max_frame_len:
                bad_byte = self._buffer.pop(0)
                logging.warning(
                    "Dropping byte 0x%02x while resynchronizing: impossible frame length %d",
                    bad_byte,
                    frame_len,
                )
                continue

            if len(self._buffer) < frame_len:
                break

            if self._buffer[frame_len - END_FLAG_LEN : frame_len] != END_FLAG:
                mac = ":".join(f"{byte:02X}" for byte in self._buffer[:MAC_LEN])
                bad_byte = self._buffer.pop(0)
                logging.warning(
                    "NUS reject mac=%s frame_type=%s declared_length=%d "
                    "actual_available=%d reason=invalid_frame_crlf "
                    "resync_byte=0x%02x",
                    mac,
                    packet_type,
                    packet_length,
                    max(0, len(self._buffer) - HEADER_LEN),
                    bad_byte,
                )
                continue

            frames.append(bytes(self._buffer[:frame_len]))
            del self._buffer[:frame_len]

        return frames

    def clear(self) -> None:
        if self._buffer:
            mac = (
                ":".join(f"{byte:02X}" for byte in self._buffer[:MAC_LEN])
                if len(self._buffer) >= MAC_LEN
                else "unknown"
            )
            packet_type = (
                bytes(self._buffer[MAC_LEN : MAC_LEN + PACKET_TYPE_LEN])
                .rstrip(b"\x00")
                .decode("ascii", errors="replace")
                if len(self._buffer) >= MAC_LEN + PACKET_TYPE_LEN
                else "unknown"
            )
            declared = (
                int.from_bytes(self._buffer[MAC_LEN + PACKET_TYPE_LEN : HEADER_LEN], "little")
                if len(self._buffer) >= HEADER_LEN
                else -1
            )
            logging.warning(
                "NUS reject mac=%s frame_type=%s declared_length=%d "
                "actual_available=%d reason=truncated_on_disconnect",
                mac,
                packet_type,
                declared,
                max(0, len(self._buffer) - HEADER_LEN),
            )
        self._buffer.clear()


def normalize_mac(mac: str) -> str:
    compact = "".join(ch for ch in mac if ch in string.hexdigits)
    if len(compact) != MAC_LEN * 2:
        raise ValueError(f"invalid MAC address: {mac!r}")
    return ":".join(compact[i : i + 2].upper() for i in range(0, len(compact), 2))


def mac_to_bytes(mac: str) -> bytes:
    normalized = normalize_mac(mac)
    return bytes(int(part, 16) for part in normalized.split(":"))


def _packet_type_bytes(packet_type: str) -> bytes:
    raw = packet_type.encode("ascii")
    if len(raw) > PACKET_TYPE_LEN:
        raise ValueError(f"packet type is longer than {PACKET_TYPE_LEN} bytes")
    return raw.ljust(PACKET_TYPE_LEN, b"\x00")


def build_frame(mac: str, packet_type: str, payload: bytes) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload is too large for uint16 length")
    return (
        mac_to_bytes(mac)
        + _packet_type_bytes(packet_type)
        + len(payload).to_bytes(PACKET_LEN_LEN, byteorder="little", signed=False)
        + payload
        + END_FLAG
    )


def validate_command(command: str) -> str:
    normalized = COMMAND_ALIASES.get(command, command)
    if normalized not in VALID_COMMANDS:
        raise ValueError("command must be one of: enter, exit")
    return normalized


def validate_record_seconds(seconds: int) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError("record seconds must be an integer from 1 to 300")
    if seconds < MIN_RECORD_SECONDS or seconds > MAX_RECORD_SECONDS:
        raise ValueError("record seconds must be an integer from 1 to 300")
    return seconds


def build_record_command(seconds: int | None = None) -> str:
    if seconds is None:
        return RECORD_COMMAND
    return f"{RECORD_COMMAND}:{validate_record_seconds(seconds)}"


def validate_sound_label(label: str) -> str:
    if not isinstance(label, str) or not SOUND_LABEL_PATTERN.fullmatch(label):
        raise ValueError(
            "sound label must be 1-24 characters using only A-Z, a-z, 0-9, _ or -"
        )
    return label


def _validate_sound_integer(
    value: int,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def build_sound_start_command(
    label: str,
    *,
    threshold_rms: int = 800,
    max_seconds: int = 60,
    silence_seconds: int = 5,
) -> str:
    label = validate_sound_label(label)
    threshold_rms = _validate_sound_integer(
        threshold_rms,
        name="sound threshold RMS",
        minimum=0,
        maximum=MAX_SOUND_THRESHOLD_RMS,
    )
    max_seconds = _validate_sound_integer(
        max_seconds,
        name="sound max seconds",
        minimum=MIN_SOUND_SECONDS,
        maximum=MAX_SOUND_SECONDS,
    )
    silence_seconds = _validate_sound_integer(
        silence_seconds,
        name="sound silence seconds",
        minimum=0,
        maximum=MAX_SOUND_SILENCE_SECONDS,
    )
    if threshold_rms == 0:
        silence_seconds = 0
    return (
        f"sound_start,label={label},thr={threshold_rms},"
        f"max={max_seconds},silence={silence_seconds}"
    )


def build_sound_background_command(
    *,
    max_seconds: int = 300,
) -> str:
    max_seconds = _validate_sound_integer(
        max_seconds,
        name="sound max seconds",
        minimum=MIN_SOUND_SECONDS,
        maximum=MAX_SOUND_SECONDS,
    )
    return f"sound_bg,max={max_seconds}"


def build_sound_auto_command(
    *,
    max_seconds: int = 300,
    silence_seconds: int = 20,
    open_db: int | None = None,
    close_db: int | None = None,
) -> str:
    max_seconds = _validate_sound_integer(
        max_seconds,
        name="sound max seconds",
        minimum=MIN_SOUND_SECONDS,
        maximum=MAX_SOUND_SECONDS,
    )
    silence_seconds = _validate_sound_integer(
        silence_seconds,
        name="sound silence seconds",
        minimum=0,
        maximum=MAX_SOUND_SILENCE_SECONDS,
    )
    if (open_db is None) != (close_db is None):
        raise ValueError("sound automatic requires both open dB and close dB overrides")
    command = f"sound_auto,max={max_seconds},silence={silence_seconds}"
    if open_db is not None and close_db is not None:
        open_db = _validate_sound_integer(
            open_db,
            name="sound open dB",
            minimum=0,
            maximum=120,
        )
        close_db = _validate_sound_integer(
            close_db,
            name="sound close dB",
            minimum=0,
            maximum=120,
        )
        if open_db <= close_db:
            raise ValueError("sound open dB must be greater than close dB")
        command += f",open_db={open_db},close_db={close_db}"
    return command


def _command_fields(command: str) -> tuple[str, dict[str, str]]:
    parts = command.split(",")
    name = parts[0]
    fields: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        if not separator or not key or key in fields:
            raise ValueError("sound command fields must be unique key=value pairs")
        fields[key] = value
    return name, fields


def _sound_int_field(fields: dict[str, str], key: str, default: int) -> int:
    value = fields.get(key)
    if value is None:
        return default
    try:
        return int(value, 10)
    except ValueError as exc:
        raise ValueError(f"sound {key} must be an integer") from exc


def validate_command_payload(command: str) -> str:
    if not isinstance(command, str) or "\x00" in command:
        raise ValueError("command must be NUL-free UTF-8 text")
    _validate_command_size(command)
    normalized = COMMAND_ALIASES.get(command, command)
    validated: str
    if normalized in VALID_COMMANDS or normalized in (RECORD_COMMAND, RECORD_STOP_COMMAND):
        validated = normalized
        return _validate_command_size(validated)
    if normalized.startswith(f"{RECORD_COMMAND}:"):
        seconds_text = normalized[len(RECORD_COMMAND) + 1 :]
        try:
            seconds = int(seconds_text, 10)
        except ValueError as exc:
            raise ValueError("record seconds must be an integer from 1 to 300") from exc
        return _validate_command_size(build_record_command(seconds))
    if normalized in (SOUND_STOP_COMMAND, SOUND_STATUS_COMMAND):
        return _validate_command_size(normalized)
    if normalized in NODE_SIMPLE_COMMANDS:
        return _validate_command_size(normalized)
    if normalized.startswith("time_sync"):
        name, fields = _command_fields(normalized)
        if name != "time_sync" or set(fields) != {"epoch_ms", "tz_min"}:
            raise ValueError("time_sync requires epoch_ms and tz_min")
        epoch_ms = _integer_field(fields, "epoch_ms")
        timezone_minutes = _integer_field(fields, "tz_min")
        if epoch_ms <= 0 or not -24 * 60 <= timezone_minutes <= 24 * 60:
            raise ValueError("invalid time_sync values")
        return _validate_command_size(
            f"time_sync,epoch_ms={epoch_ms},tz_min={timezone_minutes}"
        )
    if normalized.startswith("config_set"):
        name, fields = _command_fields(normalized)
        if name != "config_set" or set(fields) not in (
            {"location"},
            {"location", "sound_profile"},
        ):
            raise ValueError(
                "config_set requires location and optionally sound_profile"
            )
        allowed = {
            ("TOILET", "toilet_v1"),
            ("KITCHEN", "kitchen_v1"),
            ("LIVING", "living_v1"),
            ("BEDROOM", "living_v1"),
        }
        location = fields["location"].strip().upper()
        if "sound_profile" not in fields:
            if location not in {item[0] for item in allowed}:
                raise ValueError("unsupported config_set location")
            return _validate_command_size(f"config_set,location={location}")
        profile = fields["sound_profile"].strip().lower()
        if (location, profile) not in allowed:
            raise ValueError("unsupported config_set location/profile combination")
        return _validate_command_size(
            f"config_set,location={location},sound_profile={profile}"
        )
    if normalized.startswith("inout_sync"):
        name, fields = _command_fields(normalized)
        if name != "inout_sync" or set(fields) != {"bid", "state", "rid"}:
            raise ValueError("inout_sync requires bid, state and rid")
        bid = fields["bid"].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{1,32}", bid):
            raise ValueError("inout_sync bid must be hexadecimal")
        state = fields["state"].strip().lower()
        rid_text = fields["rid"].strip().lower().removeprefix("0x")
        if state not in {"in", "out"}:
            raise ValueError("invalid inout_sync state")
        if not re.fullmatch(r"[0-9a-f]{1,8}", rid_text) or int(rid_text, 16) == 0:
            raise ValueError("inout_sync rid must be a nonzero 32-bit hexadecimal value")
        return _validate_command_size(
            f"inout_sync,bid={bid},state={state},rid={rid_text}"
        )
    if normalized.startswith("sound_start"):
        name, fields = _command_fields(normalized)
        if name != "sound_start":
            raise ValueError("invalid sound_start command")
        unknown = set(fields) - {"label", "thr", "max", "silence"}
        if unknown:
            raise ValueError(f"unknown sound_start field: {sorted(unknown)[0]}")
        if "label" not in fields:
            raise ValueError("sound_start requires label")
        return _validate_command_size(
            build_sound_start_command(
                fields["label"],
                threshold_rms=_sound_int_field(fields, "thr", 800),
                max_seconds=_sound_int_field(fields, "max", 60),
                silence_seconds=_sound_int_field(fields, "silence", 5),
            )
        )
    if normalized.startswith("sound_bg"):
        name, fields = _command_fields(normalized)
        if name != "sound_bg":
            raise ValueError("invalid sound_bg command")
        unknown = set(fields) - {"max"}
        if unknown:
            raise ValueError(f"unknown sound_bg field: {sorted(unknown)[0]}")
        return _validate_command_size(
            build_sound_background_command(
                max_seconds=_sound_int_field(fields, "max", 300),
            )
        )
    if normalized.startswith("sound_auto"):
        name, fields = _command_fields(normalized)
        if name != "sound_auto":
            raise ValueError("invalid sound_auto command")
        unknown = set(fields) - {"max", "silence", "open_db", "close_db"}
        if unknown:
            raise ValueError(f"unknown sound_auto field: {sorted(unknown)[0]}")
        return _validate_command_size(
            build_sound_auto_command(
                max_seconds=_sound_int_field(fields, "max", 300),
                silence_seconds=_sound_int_field(fields, "silence", 20),
                open_db=(
                    _sound_int_field(fields, "open_db", 0)
                    if "open_db" in fields
                    else None
                ),
                close_db=(
                    _sound_int_field(fields, "close_db", 0)
                    if "close_db" in fields
                    else None
                ),
            )
        )
    raise ValueError(
        "command must be one of: record, record:<seconds>, record_stop, "
        "sound_start, sound_bg, sound_auto, sound_stop, sound_status, node_status, "
        "config_get, config_set, config_reload, time_sync, inout_sync"
    )


def build_command_frame(mac: str, command: str) -> bytes:
    return build_frame(mac, "COMMAND", validate_command_payload(command).encode("utf-8"))


def _integer_field(fields: dict[str, str], key: str) -> int:
    try:
        return int(fields[key], 10)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _validate_command_size(command: str) -> str:
    size = len(command.encode("utf-8"))
    if not MIN_COMMAND_PAYLOAD_LEN <= size <= MAX_COMMAND_PAYLOAD_LEN:
        raise ValueError(
            f"command payload must be {MIN_COMMAND_PAYLOAD_LEN}-{MAX_COMMAND_PAYLOAD_LEN} bytes"
        )
    return command


def parse_rawdata(payload: bytes) -> RawDataPacket:
    """Parse the 33-byte RAWDATA payload as a little-endian packed structure."""
    if len(payload) != RAWDATA_PAYLOAD_LEN:
        raise PacketParseError(
            f"RAWDATA payload length mismatch: expected {RAWDATA_PAYLOAD_LEN}, got {len(payload)}"
        )

    unpacked = struct.unpack("<BB7HB16b", payload)
    flag_human_presence = unpacked[0]
    detected = unpacked[1]
    (
        flag_env,
        temperature_raw,
        humidity,
        iaq,
        eco2,
        bvoc,
        accuracy,
    ) = unpacked[2:9]
    flag_sound = unpacked[9]
    sound = list(unpacked[10:])

    flags = (flag_human_presence, flag_env, flag_sound)
    if any(flag not in {0, 1} for flag in flags) or sum(flags) != 1:
        raise PacketParseError("RAWDATA must contain exactly one active producer flag")
    environment_values_are_zero = (
        temperature_raw == 0
        and humidity == 0
        and iaq == 0
        and eco2 == 0
        and bvoc == 0
        and accuracy == 0
    )
    if flag_human_presence == 1 and (
        detected not in {0, 1}
        or not environment_values_are_zero
        or any(value != 0 for value in sound)
    ):
        raise PacketParseError("PIR RAWDATA contains an invalid observation or inactive data")
    if flag_env == 1 and (
        detected != 0 or any(value != 0 for value in sound)
    ):
        raise PacketParseError("ENV RAWDATA contains inactive PIR or SOUND data")
    if flag_sound == 1 and (
        detected != 0 or not environment_values_are_zero
    ):
        raise PacketParseError("SOUND RAWDATA contains inactive PIR or ENV data")

    remaining_fields_are_zero = (
        flag_env == 0
        and temperature_raw == 0
        and humidity == 0
        and iaq == 0
        and eco2 == 0
        and bvoc == 0
        and accuracy == 0
        and flag_sound == 0
        and all(value == 0 for value in sound)
    )
    is_pir_event = flag_human_presence == 1 and remaining_fields_are_zero

    return RawDataPacket(
        flag_human_presence=flag_human_presence,
        detected=detected,
        flag_env=flag_env,
        temperature_c=temperature_raw / 100.0,
        humidity=humidity,
        iaq=iaq,
        eco2=eco2,
        bvoc=bvoc,
        accuracy=accuracy,
        flag_sound=flag_sound,
        sound=sound,
        is_pir_human_detection_event=is_pir_event,
    )


def parse_alert(payload: bytes) -> AlertPacket:
    return AlertPacket(message=payload.decode("utf-8", errors="replace"))


def parse_report(payload: bytes) -> ReportPacket:
    if len(payload) > MAX_REPORT_PAYLOAD_LEN:
        raise PacketParseError(
            f"REPORT payload exceeds {MAX_REPORT_PAYLOAD_LEN} bytes"
        )
    if b"\x00" in payload:
        raise PacketParseError("REPORT payload contains embedded NUL")
    try:
        message = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PacketParseError("REPORT payload is not valid UTF-8") from exc
    if payload.lstrip().startswith((b"{", b"[")):
        try:
            document = json.loads(message)
        except json.JSONDecodeError as exc:
            return ReportPacket(
                message=message,
                fields={},
                format="json",
                parse_error=f"invalid_json: {exc.msg}",
            )
        if not isinstance(document, dict):
            return ReportPacket(
                message=message,
                fields={},
                format="json",
                parse_error="json_report_must_be_an_object",
            )
        json_fields = {
            str(key): _json_field_text(value)
            for key, value in document.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        _normalize_report_aliases(json_fields)
        return ReportPacket(
            message=message,
            fields=json_fields,
            format="json",
            document=document,
        )
    fields: dict[str, str] = {}
    duplicate_fields: dict[str, list[str]] = {}
    for part in message.split(","):
        key, separator, value = part.partition("=")
        if separator:
            normalized_key = key.strip()
            normalized_value = value.strip()
            if normalized_key in fields:
                duplicate_fields.setdefault(normalized_key, []).append(normalized_value)
            else:
                fields[normalized_key] = normalized_value
    _normalize_report_aliases(fields)
    return ReportPacket(
        message=message,
        fields=fields,
        duplicate_fields=duplicate_fields or None,
    )


def _normalize_report_aliases(fields: dict[str, str]) -> None:
    aliases = {
        "bid": ("boot_id",),
        "cid": ("event_seq",),
        "timestamp": ("event_ts_ms", "ts"),
    }
    for canonical, alternatives in aliases.items():
        if canonical in fields:
            continue
        for alternative in alternatives:
            if alternative in fields:
                fields[canonical] = fields[alternative]
                break


def _json_field_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_frame(data: bytes) -> ParsedFrame:
    if len(data) < HEADER_LEN + END_FLAG_LEN:
        raise PacketParseError(
            f"frame too short: expected at least {HEADER_LEN + END_FLAG_LEN}, got {len(data)}"
        )

    mac_bytes = data[:MAC_LEN]
    raw_packet_type = data[MAC_LEN : MAC_LEN + PACKET_TYPE_LEN]
    packet_length = int.from_bytes(
        data[MAC_LEN + PACKET_TYPE_LEN : HEADER_LEN],
        byteorder="little",
        signed=False,
    )
    expected_len = HEADER_LEN + packet_length + END_FLAG_LEN

    if len(data) != expected_len:
        raise PacketParseError(
            f"packet length mismatch: expected frame {expected_len} bytes, got {len(data)} bytes"
        )

    if data[-END_FLAG_LEN:] != END_FLAG:
        raise PacketParseError(
            f"end flag mismatch: expected {END_FLAG.hex(' ')}, got {data[-END_FLAG_LEN:].hex(' ')}"
        )

    try:
        packet_type = raw_packet_type.rstrip(b"\x00").decode("ascii")
    except UnicodeDecodeError as exc:
        raise PacketParseError(
            f"packet type is not valid ASCII: {raw_packet_type.hex(' ')}"
        ) from exc

    payload = data[HEADER_LEN : HEADER_LEN + packet_length]
    mac = ":".join(f"{byte:02X}" for byte in mac_bytes)

    if packet_type == "RAWDATA":
        parsed: RawDataPacket | AlertPacket | ReportPacket | IgnoredPacket = parse_rawdata(payload)
    elif packet_type == "ALERT":
        parsed = parse_alert(payload)
    elif packet_type == "REPORT":
        parsed = parse_report(payload)
    elif packet_type in {"AUDIO", "WAVFILE"}:
        parsed = IgnoredPacket(packet_type=packet_type, payload_bytes=len(payload))
    else:
        raise PacketParseError(f"unknown packet type: {packet_type!r}")

    return ParsedFrame(
        mac=mac,
        mac_bytes=mac_bytes,
        packet_type=packet_type,
        packet_length=packet_length,
        payload=payload,
        parsed=parsed,
    )


def hex_dump(data: bytes) -> str:
    return data.hex(" ")


def describe_frame(frame: ParsedFrame) -> str:
    parsed = frame.parsed

    if isinstance(parsed, AlertPacket):
        return (
            f"ALERT mac={frame.mac} length={frame.packet_length} "
            f"message={parsed.message!r}"
        )

    if isinstance(parsed, ReportPacket):
        return (
            f"REPORT mac={frame.mac} length={frame.packet_length} "
            f"message={parsed.message!r}"
        )

    if isinstance(parsed, IgnoredPacket):
        return (
            f"IGNORED packet_type={parsed.packet_type} mac={frame.mac} "
            f"length={parsed.payload_bytes}"
        )

    parts = [
        f"RAWDATA mac={frame.mac}",
        f"length={frame.packet_length}",
        f"human_presence={parsed.flag_human_presence}",
        f"detected={parsed.detected}",
    ]

    if parsed.is_pir_human_detection_event:
        parts.append("event=PIR human detection")

    if parsed.flag_env == 1:
        parts.extend(
            [
                f"temperature={parsed.temperature_c:.2f} C",
                f"humidity={parsed.humidity}",
                f"iaq={parsed.iaq}",
                f"eco2={parsed.eco2}",
                f"bvoc={parsed.bvoc}",
                f"accuracy={parsed.accuracy}",
            ]
        )
    else:
        parts.append(f"flag_env={parsed.flag_env}")

    if parsed.flag_sound == 1:
        parts.append(f"sound={parsed.sound}")
    else:
        parts.append(f"flag_sound={parsed.flag_sound}")

    return ", ".join(parts)
