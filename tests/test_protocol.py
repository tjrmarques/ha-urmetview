"""Protocol tests against the byte-exact packets captured from real hardware.

Every expected value here came from a real capture and is quoted in
docs/protocol.md. These are the assertions that catch the framing mistakes the
spec calls out as easy to make - off-by-four lengths, ASCII-encoded UID
numbers, and NUL bytes left out of a length field.

    python3 -m pytest tests/ -q      (or just: uv run tests/test_protocol.py)
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(
    0,
    str(
        pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "urmetview"
    ),
)

from urmet import protocol as p  # noqa: E402
from urmet.session import _ChannelReassembler  # noqa: E402

UID = "URMABB-700171-SMCYN"


def test_short_uid_packing_is_byte_exact():
    """The numeric segment is a 3-byte big-endian int, not ASCII digits."""
    # URMABB | 000000 | 0aaf0b (=700171) | SMCYN | 000000
    expected = bytes.fromhex("55524d4142420000000aaf0b534d43594e000000")
    packed = p.pack_uid_short(UID)
    assert len(packed) == 20
    assert packed == expected
    # 700171 == 0x0aaf0b
    assert packed[9:12] == (700171).to_bytes(3, "big")


def test_long_uid_packing_carries_local_port():
    packed = p.pack_uid_long(UID, 4660)
    assert len(packed) == 36
    assert packed[:20] == p.pack_uid_short(UID)
    assert packed[20:22] == (4660).to_bytes(2, "little")


def test_bad_uid_is_rejected():
    for bad in ("nonsense", "URM-1-X", "", "URMABB_700171_SMCYN"):
        try:
            p.split_uid(bad)
        except p.ProtocolError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_data_length_counts_the_d1_prefix():
    """The classic off-by-four: the length field includes the 4-byte prefix."""
    payload = b"\xaa" * 10
    frame = p.build_data(p.CHANNEL_COMMAND, 0, payload)
    declared = int.from_bytes(frame[2:4], "big")
    assert declared == len(payload) + 4
    assert declared == len(frame) - 4


def test_data_header_layout():
    frame = p.build_data(p.CHANNEL_MEDIA, 0x1234, b"x")
    assert frame[0] == p.MAGIC
    assert frame[1] == p.MSG_DATA
    assert frame[4] == p.MSG_ACK
    assert frame[5] == p.CHANNEL_MEDIA
    assert frame[6:8] == b"\x12\x34"  # 16-bit big-endian, not a single byte


def test_command_block_length_includes_the_nul():
    block = p.build_command_block(p.SUBCMD_HELLO, "{}", 1)
    declared = int.from_bytes(block[12:16], "little")
    assert declared == 3  # "{}" plus the trailing NUL
    assert block.endswith(b"{}\x00")


def test_login_packet_matches_the_captured_bytes():
    """Reproduce the verified login packet from docs/protocol.md section 4b."""
    expected = bytes.fromhex(
        "f1d00066d1000000a3010 0ffc800010000000000030000007b7d00".replace(" ", "")
        + "a3010 0ff0b00020000000000 3f000000".replace(" ", "")
        + b'{"username":"admin","auth":"A8935C8DA4ABAD9782B7045054680D67"}\x00'.hex()
    )
    hello = p.build_command_block(p.SUBCMD_HELLO, "{}", 1)
    login = p.build_command_block(
        p.SUBCMD_LOGIN,
        '{"username":"admin","auth":"A8935C8DA4ABAD9782B7045054680D67"}',
        2,
    )
    frame = p.build_data(p.CHANNEL_COMMAND, 0, hello + login)
    assert frame == expected


def test_ack_framing_matches_captured_examples():
    """Section 1c quotes these four acks verbatim."""
    assert p.build_ack(p.CHANNEL_MEDIA, [1]) == bytes.fromhex(
        "f1d10006d101000100 01".replace(" ", "")
    )
    assert p.build_ack(p.CHANNEL_MEDIA, [2, 2]) == bytes.fromhex(
        "f1d10008d1010002000200 02".replace(" ", "")
    )
    assert p.build_ack(p.CHANNEL_MEDIA, [0x0B] * 4) == bytes.fromhex(
        "f1d1000cd1010004000b000b000b000b"
    )
    assert p.build_ack(p.CHANNEL_COMMAND, [4, 0, 0, 0]) == bytes.fromhex(
        "f1d1000cd1000004000400000000 0000".replace(" ", "")
    )


def test_empty_ack_is_none():
    assert p.build_ack(p.CHANNEL_MEDIA, []) is None


def test_trigger_block_is_a_single_zero_byte():
    block = p.build_trigger_block(p.SUBCMD_KEY, 0)
    assert int.from_bytes(block[12:16], "little") == 1
    assert block[16:] == b"\x00"
    assert block[4:6] == (p.SUBCMD_KEY).to_bytes(2, "little")


def test_parse_packet_round_trips():
    frame = p.build_data(p.CHANNEL_MEDIA, 513, b"payload")
    parsed = p.parse_packet(frame)
    assert parsed is not None
    assert parsed.channel == p.CHANNEL_MEDIA
    assert parsed.seq == 513
    assert parsed.payload == b"payload"


def test_parse_packet_ignores_non_data():
    assert p.parse_packet(p.build_simple(p.MSG_PING)) is None
    assert p.parse_packet(b"") is None
    assert p.parse_packet(b"\x00\x01\x02\x03\x04\x05\x06\x07") is None


def test_command_blocks_split_and_keep_partial_tail():
    blocks = p.build_command_block(p.SUBCMD_HELLO, "{}", 1) + p.build_command_block(
        p.SUBCMD_LOGIN, '{"auth":"ok"}', 2
    )
    parsed, rest = p.parse_command_blocks(blocks)
    assert [r.subcmd for r in parsed] == [p.SUBCMD_HELLO, p.SUBCMD_LOGIN]
    assert parsed[1].data == {"auth": "ok"}
    assert rest == b""

    # A block split across packets must not be lost.
    parsed, rest = p.parse_command_blocks(blocks[:-5])
    assert [r.subcmd for r in parsed] == [p.SUBCMD_HELLO]
    assert rest == blocks[19:-5]


def test_media_frame_header_offset():
    payload = (
        p.MARKER_MEDIA_IN + bytes([p.STREAM_VIDEO_KEYFRAME]) + b"\x00" * 27 + b"NALDATA"
    )
    media = p.parse_media_start(payload)
    assert media is not None
    assert media.is_video and media.is_keyframe
    assert media.data == b"NALDATA"

    audio = p.parse_media_start(
        p.MARKER_MEDIA_IN + bytes([p.STREAM_AUDIO]) + b"\x00" * 27 + b"\xff" * 4
    )
    assert audio is not None and audio.is_audio and not audio.is_video


def test_pframes_count_as_video():
    """0x02 must be merged with 0x01, or P-frames have nothing to reference."""
    payload = p.MARKER_MEDIA_IN + bytes([p.STREAM_VIDEO_PFRAME]) + b"\x00" * 27 + b"P"
    media = p.parse_media_start(payload)
    assert media is not None
    assert media.is_video
    assert not media.is_keyframe


def test_audio_out_frame_is_404_bytes_on_the_wire():
    frame = p.build_audio_out_frame(b"\xff" * 320)
    assert len(frame) == 396  # + 8-byte outer header = 404
    assert frame[:4] == p.MARKER_AUDIO_OUT
    assert int.from_bytes(frame[8:12], "little") == 320
    assert frame[76:] == b"\xff" * 320


def test_audio_out_rejects_wrong_size():
    for bad in (b"", b"\xff" * 319, b"\xff" * 321):
        try:
            p.build_audio_out_frame(bad)
        except p.ProtocolError:
            continue
        raise AssertionError("wrong-sized talk frame should be rejected")


def test_private_ip_detection_picks_the_lan_candidate():
    assert p.is_private_ip("10.0.50.6")
    assert p.is_private_ip("192.168.1.4")
    assert p.is_private_ip("172.16.0.1")
    assert not p.is_private_ip("172.32.0.1")
    assert not p.is_private_ip("3.121.150.135")


def test_reassembler_orders_dedups_and_survives_wraparound():
    r = _ChannelReassembler()
    assert r.push(0, b"a") == [b"a"]
    # Out of order: 2 is held until 1 arrives.
    assert r.push(2, b"c") == []
    assert r.push(1, b"b") == [b"b", b"c"]
    # A retransmit of already-emitted data is discarded.
    assert r.push(1, b"b") == []

    wrap = _ChannelReassembler()
    assert wrap.push(0xFFFF, b"x") == [b"x"]
    assert wrap.push(0, b"y") == [b"y"]


def test_reassembler_skips_a_permanent_gap():
    """A packet that never arrives must not stall the stream forever."""
    r = _ChannelReassembler()
    r.push(0, b"start")
    for seq in range(2, 2 + 300):
        r.push(seq, bytes([seq & 0xFF]))
    assert r.held < 300  # it gave up on seq 1 and moved on


def _run_standalone() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {err}")
        else:
            print(f"ok   {name}")
    print("\n" + ("all passed" if not failures else f"{failures} failed"))
    return 1 if failures else 0


def test_both_cloud_uid_packings_are_36_bytes_and_differ():
    """Two layouts exist; the live servers accept both.

    Spec section 2b puts the port at offset 20, urmet_client.py put it at 22.
    Tested against the real servers: both are accepted, so the integration
    sends only the documented offset-20 form. This keeps the other honest in
    case a future firmware becomes stricter.
    """
    spec = p.pack_uid_long(UID, 0x1234)
    alt = p.pack_uid_long_alt(UID, 0x1234)
    assert len(spec) == len(alt) == 36
    assert spec != alt
    # Same identity, different port placement.
    assert spec[:20] == p.pack_uid_short(UID)
    assert spec[20:22] == b"\x34\x12"
    assert alt[22:24] == b"\x34\x12"
    assert alt[:17] == spec[:17]


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
