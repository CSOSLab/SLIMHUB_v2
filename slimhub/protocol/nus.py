from __future__ import annotations

import logging
import string
import struct
from dataclasses import dataclass


NUS_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_RX_WRITE_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX_NOTIFY_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

DEFAULT_DEVICE_NAME = "DEAN_NODE_V2"
END_FLAG = b"\x0d\x0a"
MAC_LEN = 6
PACKET_TYPE_LEN = 8
PACKET_LEN_LEN = 2
HEADER_LEN = MAC_LEN + PACKET_TYPE_LEN + PACKET_LEN_LEN
END_FLAG_LEN = len(END_FLAG)
RAWDATA_PAYLOAD_LEN = 33
MAX_FRAME_LEN = 4096


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
class ParsedFrame:
    mac: str
    mac_bytes: bytes
    packet_type: str
    packet_length: int
    payload: bytes
    parsed: RawDataPacket | AlertPacket


class FrameAssembler:
    """Accumulates BLE notify chunks until a complete length-delimited frame exists."""

    def __init__(self, max_frame_len: int = MAX_FRAME_LEN) -> None:
        self._buffer = bytearray()
        self._max_frame_len = max_frame_len

    def push(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        frames: list[bytes] = []

        while len(self._buffer) >= HEADER_LEN + END_FLAG_LEN:
            packet_length = int.from_bytes(
                self._buffer[MAC_LEN + PACKET_TYPE_LEN : HEADER_LEN],
                byteorder="little",
                signed=False,
            )
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

            frames.append(bytes(self._buffer[:frame_len]))
            del self._buffer[:frame_len]

        return frames

    def clear(self) -> None:
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


def build_command_frame(mac: str, command: str) -> bytes:
    return build_frame(mac, "COMMAND", command.encode("utf-8"))


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
    is_pir_event = (
        flag_human_presence == 1 and detected == 1 and remaining_fields_are_zero
    )

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
        parsed: RawDataPacket | AlertPacket = parse_rawdata(payload)
    elif packet_type == "ALERT":
        parsed = parse_alert(payload)
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
