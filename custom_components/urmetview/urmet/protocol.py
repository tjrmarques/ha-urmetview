"""Wire protocol for the Urmet Kit 1730/67 (UrmetView) intercom.

Pure functions and constants only - no I/O, no Home Assistant imports. Every
layout here is documented in ``docs/protocol.md``; section references in the
comments point back at it.

The three things that are easy to get wrong, all of which are encoded here
rather than left to callers:

* ``d0`` length fields count the 4-byte ``d1 <channel> <seq>`` prefix
  (section 1b).
* The outer sequence is 16-bit big-endian and offset 5 is the channel, not
  padding (section 1b).
* JSON command payload lengths include the trailing NUL byte (section 3).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

# --- Message types (outer header) -------------------------------------------

MAGIC = 0xF1

MSG_CHECKCAM = 0x41
MSG_SESSION_ACK = 0x42
MSG_DATA = 0xD0
MSG_ACK = 0xD1
MSG_PING = 0xE0
MSG_PING_ACK = 0xE1
MSG_CLOUD_HELLO = 0x00
MSG_CLOUD_LOOKUP = 0x20
MSG_CLOUD_STATUS = 0x21
MSG_CLOUD_CANDIDATE = 0x40

CHANNEL_COMMAND = 0x00
CHANNEL_MEDIA = 0x01

# --- Payload markers --------------------------------------------------------

MARKER_CMD = b"\xa3\x01\x00\xff"
MARKER_MEDIA_IN = b"\xa5\x01\x00\xff"
MARKER_AUDIO_OUT = b"\xa7\x01\x00\xff"

MEDIA_HEADER_LEN = 32  # marker + 28 bytes of frame info, mostly unmapped
AUDIO_OUT_HEADER_LEN = 76  # marker + constant + length + 64 bytes padding

#: 40ms of 8kHz mono mu-law. Defined here rather than imported from const so
#: this module stays self-contained; const re-exports the same value.
AUDIO_FRAME_BYTES = 320

STREAM_VIDEO_KEYFRAME = 0x01
STREAM_VIDEO_PFRAME = 0x02
STREAM_AUDIO = 0x08
VIDEO_STREAM_TYPES = (STREAM_VIDEO_KEYFRAME, STREAM_VIDEO_PFRAME)

# --- Sub-commands (2-byte little-endian, inside an a3 block) ----------------

SUBCMD_HELLO = 0x00C8
SUBCMD_LOGIN = 0x000B
SUBCMD_DATETIME = 0x00CA
SUBCMD_VIDEO_START = 0x0064
SUBCMD_VIDEO_STOP = 0x0065
SUBCMD_AUDIO_START = 0x0066
SUBCMD_AUDIO_STOP = 0x0067
SUBCMD_TALK_CHANNEL_ON = 0x0068
SUBCMD_TALK_ACTION = 0x0069
SUBCMD_TALK_CHANNEL_OFF = 0x006A
SUBCMD_STREAM_CONFIG = 0x00CD
SUBCMD_GET_INFO = 0x0386
SUBCMD_KEY = 0x07D0
SUBCMD_GATE = 0x07D1
SUBCMD_SELECT_UNIT = 0x07D2

#: App label -> wire value. Note that this changes frame rate/bitrate, not
#: resolution: the stream is 960x240 in every mode (spec section 4d).
QUALITY_VALUES = {"ld": "5", "sd": "1", "hd": "6"}

# --- Cloud rendezvous -------------------------------------------------------

CLOUD_HOSTS = ("p2p1.caycctv.com", "p2p2.caycctv.com", "p2p3.caycctv.com")
#: Fallback IPs, used only if DNS fails. Urmet may rotate these.
CLOUD_FALLBACK_IPS = ("3.121.150.135", "15.161.180.1", "35.181.124.200")
CLOUD_PORT = 32100

#: The device announces itself here periodically, in cleartext.
DISCOVERY_PORT = 6688
DISCOVERY_PACKET_MIN_LEN = 330

UID_RE = re.compile(r"^([A-Z]{6})-(\d{1,8})-([A-Z]{5})$")
AUTH_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class ProtocolError(Exception):
    """Raised when a packet cannot be parsed or a UID cannot be packed."""


# --- UID packing (section 2) ------------------------------------------------


def split_uid(uid: str) -> tuple[str, int, str]:
    """Split ``PREFIX-NUMBER-SUFFIX`` into its parts.

    Raises ProtocolError rather than ValueError so callers have one exception
    type to handle across the whole protocol layer.
    """
    match = UID_RE.match(uid.strip().upper())
    if not match:
        raise ProtocolError(
            f"UID {uid!r} does not match the expected PREFIX-NUMBER-SUFFIX format"
        )
    prefix, number, suffix = match.groups()
    return prefix, int(number), suffix


def pack_uid_short(uid: str) -> bytes:
    """20-byte form used by checkCam (0x41) and the session ack (0x42).

    The numeric segment is a 3-byte big-endian integer, *not* ASCII digits -
    this is the single most common packing mistake with this protocol.
    """
    prefix, number, suffix = split_uid(uid)
    return (
        prefix.encode("ascii")
        + b"\x00\x00\x00"
        + number.to_bytes(3, "big")
        + suffix.encode("ascii")
        + b"\x00\x00\x00"
    )


def pack_uid_long(uid: str, local_port: int) -> bytes:
    """36-byte form used by the cloud lookup request (section 2b)."""
    return (
        pack_uid_short(uid)
        + local_port.to_bytes(2, "little")
        + b"\x00" * 14
    )


# --- Outer framing (section 1) ----------------------------------------------


def build_simple(msg_type: int, payload: bytes = b"") -> bytes:
    """``f1 <type> <len 2B BE> <payload>`` - checkCam, ping, cloud messages."""
    return bytes([MAGIC, msg_type]) + len(payload).to_bytes(2, "big") + payload


def build_data(channel: int, seq: int, payload: bytes) -> bytes:
    """``f1 d0 <len 2B BE> d1 <channel> <seq 2B BE> <payload>``.

    The length field counts the 4-byte ``d1`` prefix as well as the payload.
    """
    body = bytes([MSG_ACK, channel]) + (seq & 0xFFFF).to_bytes(2, "big") + payload
    return bytes([MAGIC, MSG_DATA]) + len(body).to_bytes(2, "big") + body


def build_ack(channel: int, seqs: Iterable[int]) -> bytes | None:
    """``f1 d1 <len 2B BE> d1 <channel> 00 <count> <seq 2B BE>*count``.

    Returns None for an empty sequence list so callers can send unconditionally.
    """
    seq_list = list(seqs)
    if not seq_list:
        return None
    if len(seq_list) > 255:
        raise ProtocolError("cannot ack more than 255 sequences in one packet")
    body = bytes([MSG_ACK, channel, 0x00, len(seq_list)])
    for seq in seq_list:
        body += (seq & 0xFFFF).to_bytes(2, "big")
    return bytes([MAGIC, MSG_ACK]) + len(body).to_bytes(2, "big") + body


@dataclass(frozen=True)
class DataPacket:
    """A parsed ``f1 d0`` packet."""

    channel: int
    seq: int
    payload: bytes


def parse_packet(data: bytes) -> DataPacket | None:
    """Parse an inbound ``d0`` frame, or return None if this isn't one.

    Non-``d0`` traffic (ping acks, session acks) carries no payload we act on,
    so it is filtered out here rather than at every call site.
    """
    if len(data) < 8 or data[0] != MAGIC or data[1] != MSG_DATA:
        return None
    if data[4] != MSG_ACK:
        return None
    return DataPacket(channel=data[5], seq=int.from_bytes(data[6:8], "big"), payload=data[8:])


# --- Command blocks (section 3) ---------------------------------------------


def build_command_block(subcmd: int, payload_text: str, seq: int) -> bytes:
    """One ``a3``-marker command block.

    ``payload_text`` is normally JSON, but the ``86 03`` info requests use a
    literal ``GET /path`` string in the same framing. Either way it is
    NUL-terminated and the length field counts that NUL.
    """
    body = payload_text.encode("utf-8") + b"\x00"
    return (
        MARKER_CMD
        + (subcmd & 0xFFFF).to_bytes(2, "little")
        + (seq & 0xFFFF).to_bytes(2, "little")
        + b"\x00\x00\x00\x00"
        + len(body).to_bytes(4, "little")
        + body
    )


def build_trigger_block(subcmd: int, seq: int) -> bytes:
    """Parameterless trigger (unit-select, key, gate): a single 0x00 payload."""
    return (
        MARKER_CMD
        + (subcmd & 0xFFFF).to_bytes(2, "little")
        + (seq & 0xFFFF).to_bytes(2, "little")
        + b"\x00\x00\x00\x00"
        + (1).to_bytes(4, "little")
        + b"\x00"
    )


@dataclass(frozen=True)
class CommandResponse:
    """One decoded ``a3`` block from the device."""

    subcmd: int
    seq: int
    text: str

    @property
    def data(self) -> dict[str, Any]:
        """The block's JSON body, or ``{}`` for the XML/GET-style responses."""
        try:
            parsed = json.loads(self.text)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


def parse_command_blocks(buffer: bytes) -> tuple[list[CommandResponse], bytes]:
    """Split a command-channel byte stream into complete blocks.

    Returns the decoded blocks plus whatever trailing bytes form an incomplete
    block, which the caller should prepend to the next chunk. Command
    responses are small enough to arrive in one packet in practice, but the
    device is free to split them and a partial block must not be discarded.
    """
    responses: list[CommandResponse] = []
    offset = 0
    while len(buffer) - offset >= 16:
        if buffer[offset : offset + 4] != MARKER_CMD:
            # Not at a block boundary. Resynchronise on the next marker rather
            # than dropping the whole buffer - a single corrupt/unknown block
            # should not cost us the ones after it.
            next_marker = buffer.find(MARKER_CMD, offset + 1)
            if next_marker == -1:
                return responses, b""
            offset = next_marker
            continue
        subcmd = int.from_bytes(buffer[offset + 4 : offset + 6], "little")
        seq = int.from_bytes(buffer[offset + 6 : offset + 8], "little")
        length = int.from_bytes(buffer[offset + 12 : offset + 16], "little")
        end = offset + 16 + length
        if length > 1 << 20:
            # Implausible length: treat as desync and hunt for the next marker.
            next_marker = buffer.find(MARKER_CMD, offset + 1)
            if next_marker == -1:
                return responses, b""
            offset = next_marker
            continue
        if end > len(buffer):
            break
        text = buffer[offset + 16 : end].split(b"\x00", 1)[0].decode("utf-8", "replace")
        responses.append(CommandResponse(subcmd=subcmd, seq=seq, text=text))
        offset = end
    return responses, buffer[offset:]


# --- Media framing (section 5) ----------------------------------------------


@dataclass(frozen=True)
class MediaFrameStart:
    """The first packet of a media frame."""

    stream_type: int
    data: bytes

    @property
    def is_video(self) -> bool:
        return self.stream_type in VIDEO_STREAM_TYPES

    @property
    def is_keyframe(self) -> bool:
        return self.stream_type == STREAM_VIDEO_KEYFRAME

    @property
    def is_audio(self) -> bool:
        return self.stream_type == STREAM_AUDIO


def parse_media_start(payload: bytes) -> MediaFrameStart | None:
    """Return the frame header if this payload opens a media frame.

    Continuation packets have no marker - the caller appends them to whichever
    stream was most recently opened.
    """
    if len(payload) < 5 or payload[0:4] != MARKER_MEDIA_IN:
        return None
    return MediaFrameStart(stream_type=payload[4], data=payload[MEDIA_HEADER_LEN:])


def build_audio_out_frame(pcmu: bytes) -> bytes:
    """Wrap 320 bytes of mu-law in the outgoing-talk header (section 5b).

    The 84-byte total offset before audio data (8 outer + 76 here) matters: an
    off-by-16 here decodes one padding byte as a full-amplitude mu-law sample
    at every frame boundary, which is audible as periodic hammering.
    """
    if len(pcmu) != AUDIO_FRAME_BYTES:
        raise ProtocolError(
            f"talk audio frames must be exactly {AUDIO_FRAME_BYTES} bytes, got {len(pcmu)}"
        )
    return (
        MARKER_AUDIO_OUT
        + b"\x89\x00\x00\x00"
        + len(pcmu).to_bytes(4, "little")
        + b"\x00" * 64
        + pcmu
    )


# --- Cloud rendezvous (section 4a) ------------------------------------------


def build_cloud_hello() -> bytes:
    return build_simple(MSG_CLOUD_HELLO)


def build_cloud_lookup(uid: str, local_port: int) -> bytes:
    return build_simple(MSG_CLOUD_LOOKUP, pack_uid_long(uid, local_port))


@dataclass(frozen=True)
class CloudResponse:
    """A decoded cloud rendezvous reply."""

    msg_type: int
    host: str | None
    port: int | None
    status: int | None = None

    @property
    def is_candidate(self) -> bool:
        return self.msg_type == MSG_CLOUD_CANDIDATE


def parse_cloud_response(data: bytes) -> CloudResponse | None:
    """Decode one cloud reply; None if it isn't one we understand."""
    if len(data) < 4 or data[0] != MAGIC:
        return None
    msg_type = data[1]
    if msg_type == MSG_CLOUD_STATUS and len(data) >= 8:
        return CloudResponse(msg_type=msg_type, host=None, port=None, status=data[4])
    if len(data) < 20:
        return None
    port = int.from_bytes(data[6:8], "little")
    host = ".".join(str(b) for b in reversed(data[8:12]))
    return CloudResponse(msg_type=msg_type, host=host, port=port)


def is_private_ip(host: str) -> bool:
    """True for RFC1918 addresses - the LAN candidate is the one that works."""
    try:
        first, second, *_ = (int(part) for part in host.split("."))
    except ValueError:
        return False
    if first == 10:
        return True
    if first == 192 and second == 168:
        return True
    return first == 172 and 16 <= second <= 31


# --- LAN discovery broadcast (section 2c) -----------------------------------


@dataclass(frozen=True)
class DiscoveredDevice:
    """Fields lifted from the device's periodic UDP 6688 announcement."""

    uid: str
    host: str
    mac: str
    verification_code: str
    firmware: str
    password: str


def _ascii_field(data: bytes, start: int, length: int) -> str:
    return data[start : start + length].split(b"\x00", 1)[0].decode("ascii", "replace").strip()


def parse_discovery_broadcast(data: bytes) -> DiscoveredDevice | None:
    """Decode the LAN announcement, or None if it isn't one.

    Note this packet contains the device password in cleartext - see the
    security section of the protocol doc. We read it for display during setup
    but never persist it, since the login credential is the auth hash.
    """
    if len(data) < DISCOVERY_PACKET_MIN_LEN or data[0:4] != b"\x22\x11\x01\x08":
        return None
    uid = _ascii_field(data, 108, 24)
    if not UID_RE.match(uid):
        return None
    mac = ":".join(f"{b:02x}" for b in data[100:106])
    return DiscoveredDevice(
        uid=uid,
        host=_ascii_field(data, 84, 9),
        mac=mac,
        verification_code=_ascii_field(data, 132, 20),
        firmware=_ascii_field(data, 232, 11),
        password=_ascii_field(data, 296, 32),
    )
