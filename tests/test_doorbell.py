"""Doorbell detection tests, built on the real ring packet from urmet5.

The ring bytes below were captured from the device at the moment the bell was
pressed. The distinction these tests protect is the one that matters: `f1 f9`
is the ring, `f1 12` is periodic registration every ~33s. Confusing the two
would fire the doorbell twice a minute forever.
"""

from __future__ import annotations

import pathlib
import socket
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "urmetview"))

from doorbell import (  # noqa: E402
    RING_DEBOUNCE,
    DoorbellListener,
    parse_udp,
    strip_tzsp,
)

DEVICE_IP = "10.0.50.6"
CLOUD_IP = "35.181.124.200"

# First 40 bytes of the real ring packet, zero-padded to its captured length of
# 88. Only the first two bytes are parsed; the rest is opaque/encrypted.
RING_PAYLOAD = bytes.fromhex(
    "f1f90054" "15fd19a0d1e84208793c4d3aef6f126bb33d84752ff672b2c582cbd8eab85a28b72731 11"
    .replace(" ", "")
).ljust(88, b"\x00")

# The periodic registration message, from the same capture.
REGISTER_PAYLOAD = bytes.fromhex(
    "f112002c" "4824192bc6cef2547c4863f7c7ffab2ba00ca09e6e462220d2d2dae88e94cce6bade94d2"
).ljust(48, b"\x00")


def build_frame(payload: bytes, src: str = DEVICE_IP, dport: int = 32100, sport: int = 11451) -> bytes:
    """Wrap a payload in Ethernet/IPv4/UDP, the way the mirror delivers it."""
    udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = (
        bytes([0x45, 0x00])
        + struct.pack(">H", 20 + len(udp))
        + b"\x00\x00\x00\x00\x40"
        + bytes([socket.IPPROTO_UDP])
        + b"\x00\x00"
        + bytes(int(o) for o in src.split("."))
        + bytes(int(o) for o in CLOUD_IP.split("."))
    )
    return b"\xff" * 6 + b"\x11" * 6 + b"\x08\x00" + ip + udp


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


def test_tzsp_header_is_stripped():
    frame = build_frame(RING_PAYLOAD)
    assert strip_tzsp(build_tzsp(frame)) == frame
    assert strip_tzsp(b"") is None
    assert strip_tzsp(b"\x99\x00\x00\x01\x01") is None  # wrong version


def test_udp_parsing_recovers_the_payload():
    parsed = parse_udp(build_frame(RING_PAYLOAD))
    assert parsed is not None
    src, sport, _dst, dport, payload = parsed
    assert src == DEVICE_IP
    assert sport == 11451
    assert dport == 32100
    assert payload == RING_PAYLOAD


def test_the_real_ring_packet_fires_the_doorbell():
    harness = _Harness()
    harness.feed(RING_PAYLOAD)
    assert harness.rings == 1


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


def test_retransmissions_collapse_into_one_ring():
    """The ring goes to three servers and is retransmitted - ~9 packets."""
    harness = _Harness()
    for _ in range(9):
        harness.feed(RING_PAYLOAD)
    assert harness.rings == 1


def test_a_later_ring_is_reported_again():
    harness = _Harness()
    harness.feed(RING_PAYLOAD)
    # Move the debounce window into the past rather than sleeping.
    harness.listener._last_ring -= RING_DEBOUNCE + 1
    harness.feed(RING_PAYLOAD)
    assert harness.rings == 2


def test_traffic_from_other_hosts_is_ignored():
    harness = _Harness()
    harness.feed(RING_PAYLOAD, src="10.0.20.113")
    assert harness.rings == 0


def test_non_cloud_traffic_is_ignored():
    """Only device->cloud control traffic can carry a ring."""
    harness = _Harness()
    harness.feed(RING_PAYLOAD, dport=1234)
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
    listener.datagram_received(build_tzsp(build_frame(RING_PAYLOAD)), ("10.0.0.1", 37008))
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
