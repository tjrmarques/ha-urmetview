"""The standalone login script must build the same bytes as the integration.

tools/urmet_login.py deliberately duplicates the framing so it runs on a
laptop with nothing installed and no checkout. A duplicate that drifts is
worse than no duplicate at all: it would fail against the device and send us
hunting for a network fault that does not exist. This pins the two together.

    uv run tests/test_standalone.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "urmetview"))

from urmet import protocol as p  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "solo", ROOT / "tools" / "urmet_login.py"
)
solo = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(solo)

UID = "URMABB-700171-SMCYN"
AUTH = "0123456789abcdef0123456789abcdef"


def test_uid_packing_matches() -> None:
    assert solo.pack_uid(UID) == p.pack_uid_short(UID)


def test_simple_frames_match() -> None:
    assert solo.simple(solo.MSG_CHECKCAM, solo.pack_uid(UID)) == p.build_simple(
        p.MSG_CHECKCAM, p.pack_uid_short(UID)
    )
    assert solo.simple(solo.MSG_PING) == p.build_simple(p.MSG_PING)
    assert solo.simple(solo.MSG_LAN_SEARCH) == p.build_simple(0x30)


def test_data_and_ack_framing_match() -> None:
    """The d0 length counts the 4-byte d1 prefix - the classic off-by-four."""
    assert solo.data_frame(0, 7, b"hello") == p.build_data(0, 7, b"hello")
    assert solo.ack_frame(0, [7]) == p.build_ack(0, [7])


def test_command_blocks_match() -> None:
    """Little-endian subcmd/seq, and a length that includes the trailing NUL."""
    assert solo.command_block(solo.SUBCMD_HELLO, "{}", 0) == p.build_command_block(
        p.SUBCMD_HELLO, "{}", 0
    )
    text = '{"username":"admin","auth":"%s"}' % AUTH
    assert solo.command_block(solo.SUBCMD_LOGIN, text, 1) == p.build_command_block(
        p.SUBCMD_LOGIN, text, 1
    )


def test_subcommand_numbers_match() -> None:
    for name in (
        "SUBCMD_HELLO",
        "SUBCMD_LOGIN",
        "SUBCMD_VIDEO_STOP",
        "SUBCMD_AUDIO_STOP",
        "SUBCMD_TALK_ACTION",
        "SUBCMD_TALK_CHANNEL_OFF",
    ):
        assert getattr(solo, name) == getattr(p, name), name


def test_parsers_read_what_the_integration_writes() -> None:
    blocks = p.build_command_block(p.SUBCMD_LOGIN, '{"auth":"ok"}', 3)
    parsed, rest = solo.parse_blocks(blocks)
    assert parsed == [(p.SUBCMD_LOGIN, '{"auth":"ok"}')]
    assert rest == b""
    assert solo.parse_data(p.build_data(0, 42, b"payload")) == (0, 42, b"payload")


def test_partial_block_is_kept_not_dropped() -> None:
    blocks = p.build_command_block(p.SUBCMD_LOGIN, '{"auth":"ok"}', 3)
    parsed, rest = solo.parse_blocks(blocks[:-4])
    assert parsed == []
    assert rest == blocks[:-4]


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


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
