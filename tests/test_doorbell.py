"""Doorbell detection tests, grounded in the urmet5 capture.

That capture contained exactly one ring, at a known time, and three candidate
signals. These tests pin down which is which:

* TCP SYN to port 32002 - at the button press. The trigger. Present in all
  three ring captures (+11.401s, +7.590s, +13.552s).
* `f1 f9` - only in urmet5, 22s late, absent from the other two ring
  captures. The call going unanswered. Must NOT fire.
* `f1 12` - every ~33s forever, registration. Must NOT fire.

The last two are the traps: both look event-shaped in one short capture, and
an earlier revision triggered on `f1 f9`, which would have made the doorbell
22 seconds late and silent whenever the call was answered.
"""

from __future__ import annotations

import pathlib
import socket
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "urmetview"))

from doorbell import (  # noqa: E402
    PUSH_PORT,
    RING_DEBOUNCE,
    DoorbellListener,
    parse_packet,
    strip_tzsp,
)

DEVICE_IP = "10.0.50.6"
CLOUD_IP = "35.181.124.200"

PUSH_SERVER = "54.84.37.235"

# The f1 f9 message, captured 22s AFTER the button press.
UNANSWERED_PAYLOAD = bytes.fromhex(
    "f1f90054" "15fd19a0d1e84208793c4d3aef6f126bb33d84752ff672b2c582cbd8eab85a28b72731 11"
    .replace(" ", "")
).ljust(88, b"\x00")

# The periodic registration message, from the same capture.
REGISTER_PAYLOAD = bytes.fromhex(
    "f112002c" "4824192bc6cef2547c4863f7c7ffab2ba00ca09e6e462220d2d2dae88e94cce6bade94d2"
).ljust(48, b"\x00")


def _ip_frame(proto: int, body: bytes, src: str, dst: str) -> bytes:
    ip = (
        bytes([0x45, 0x00])
        + struct.pack(">H", 20 + len(body))
        + b"\x00\x00\x00\x00\x40"
        + bytes([proto])
        + b"\x00\x00"
        + bytes(int(o) for o in src.split("."))
        + bytes(int(o) for o in dst.split("."))
    )
    return b"\xff" * 6 + b"\x11" * 6 + b"\x08\x00" + ip + body


def build_frame(
    payload: bytes, src: str = DEVICE_IP, dport: int = 32100, sport: int = 11451
) -> bytes:
    """Wrap a payload in Ethernet/IPv4/UDP, the way the mirror delivers it."""
    udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
    return _ip_frame(socket.IPPROTO_UDP, udp, src, CLOUD_IP)


def build_tcp(
    dport: int = PUSH_PORT,
    flags: int = 0x02,
    src: str = DEVICE_IP,
    dst: str = PUSH_SERVER,
    sport: int = 46548,
) -> bytes:
    """A TCP segment; default is the SYN that opens the push connection."""
    tcp = (
        struct.pack(">HH", sport, dport)
        + b"\x00" * 8
        + bytes([0x50, flags])
        + b"\x00" * 6
    )
    return _ip_frame(socket.IPPROTO_TCP, tcp, src, dst)


def build_tzsp(frame: bytes) -> bytes:
    """version=1, type=0 (received), encap=ethernet, END tag, then the frame."""
    return bytes([0x01, 0x00, 0x00, 0x01, 0x01]) + frame


class _Harness:
    def __init__(self, device_ip: str | None = DEVICE_IP) -> None:
        self.rings = 0
        self.ports: list[int] = []
        self.listener = DoorbellListener(device_ip, self._ring, self.ports.append)

    def _ring(self) -> None:
        self.rings += 1

    def feed(self, payload: bytes, **kwargs) -> None:
        self.listener.datagram_received(
            build_tzsp(build_frame(payload, **kwargs)), ("10.0.0.1", 37008)
        )

    def feed_tcp(self, **kwargs) -> None:
        self.listener.datagram_received(
            build_tzsp(build_tcp(**kwargs)), ("10.0.0.1", 37008)
        )


def test_tzsp_header_is_stripped():
    frame = build_frame(UNANSWERED_PAYLOAD)
    assert strip_tzsp(build_tzsp(frame)) == frame
    assert strip_tzsp(b"") is None
    assert strip_tzsp(b"\x99\x00\x00\x01\x01") is None  # wrong version


def test_udp_parsing_recovers_the_payload():
    packet = parse_packet(build_frame(UNANSWERED_PAYLOAD))
    assert packet is not None
    assert packet.src == DEVICE_IP
    assert packet.sport == 11451
    assert packet.dport == 32100
    assert packet.payload == UNANSWERED_PAYLOAD


def test_tcp_parsing_reads_flags():
    syn = parse_packet(build_tcp(flags=0x02))
    assert syn is not None and syn.is_syn
    # An established connection is not a new ring.
    assert not parse_packet(build_tcp(flags=0x12)).is_syn
    assert not parse_packet(build_tcp(flags=0x10)).is_syn


def test_the_push_connection_fires_the_doorbell():
    """The real trigger: a SYN to the push service, at the button press."""
    harness = _Harness()
    harness.feed_tcp()
    assert harness.rings == 1


def test_the_unanswered_message_does_not_fire():
    """f1 f9 lands 22s after the press - as a doorbell it would be useless."""
    harness = _Harness()
    harness.feed(UNANSWERED_PAYLOAD)
    assert harness.rings == 0
    assert harness.listener.unanswered == 1


def test_other_tcp_destinations_are_ignored():
    harness = _Harness()
    harness.feed_tcp(dport=443)
    harness.feed_tcp(dport=80)
    assert harness.rings == 0


def test_registration_never_fires_the_doorbell():
    """The whole point: f1 12 arrives every ~33s and must be ignored."""
    harness = _Harness()
    for _ in range(20):
        harness.feed(REGISTER_PAYLOAD)
    assert harness.rings == 0


def test_registration_port_is_captured_for_discovery():
    harness = _Harness()
    harness.feed(REGISTER_PAYLOAD, sport=10492)
    assert harness.ports == [10492]


def test_both_push_servers_collapse_into_one_ring():
    """One ring = two SYNs.

    The device races two push providers, opening both connections in the same
    millisecond and hanging up on whichever greets it second. Both SYNs are
    always present, so both must fold into a single event.
    """
    harness = _Harness()
    harness.feed_tcp(dst="54.84.37.235")
    harness.feed_tcp(dst="139.59.110.98")
    for _ in range(5):  # SYN retransmits
        harness.feed_tcp()
    assert harness.rings == 1


def test_a_later_ring_is_reported_again():
    harness = _Harness()
    harness.feed_tcp()
    # Move the debounce window into the past rather than sleeping.
    harness.listener._last_ring -= RING_DEBOUNCE + 1
    harness.feed_tcp()
    assert harness.rings == 2


def test_traffic_from_other_hosts_is_ignored():
    harness = _Harness()
    harness.feed_tcp(src="10.0.20.113")
    assert harness.rings == 0


def test_garbage_does_not_raise():
    harness = _Harness()
    for junk in (b"", b"\x01", b"\x01\x00\x00\x01\x01", b"\x01\x00\x00\x01\x01" + b"\xff" * 40):
        harness.listener.datagram_received(junk, ("10.0.0.1", 37008))
    assert harness.rings == 0


def test_callback_errors_do_not_kill_the_listener():
    """A broken consumer must not take the socket down with it."""

    def boom() -> None:
        raise RuntimeError("entity exploded")

    listener = DoorbellListener(DEVICE_IP, boom)
    listener.datagram_received(build_tzsp(build_tcp()), ("10.0.0.1", 37008))
    assert listener.rings == 1  # counted, and no exception escaped


def _run_standalone() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {err!r}")
        else:
            print(f"ok   {name}")
    print("\n" + ("all passed" if not failures else f"{failures} failed"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
