"""Doorbell detection by mirroring the device's cloud-bound traffic.

When the bell rings the device sends **nothing on the LAN** - the alert is a
cloud push addressed to registered smartphones, which Home Assistant cannot
receive. But the device's own outbound traffic gives it away, and a router that
can mirror turns that into a local event.

**TCP to port 32002** is the ring. Confirmed across three independent ring
captures - the device opened exactly one connection to the same two push
servers in each, at the moment the button was pressed:

===========  ==================  ==========
capture      TCP SYN to :32002   ``f1 f9``
===========  ==================  ==========
urmet3       +11.401s            absent
urmet4       +7.590s             absent
urmet5       +13.552s            +35.3s
===========  ==================  ==========

It is also the *only* TCP the device ever makes - 75-78 packets per capture,
all port 32002 - so matching on it cannot collide with anything else.

Two decoys, both of which an earlier revision of this file fell for:

* ``f1 f9`` appears only in urmet5, 22s after the press, and is absent from
  the other two ring captures. It is the call going unanswered, not the ring.
  Triggering on it gives a doorbell that is 22 seconds late.
* ``f1 12`` fires every ~33 seconds forever - periodic registration.

Set up on a MikroTik. Note the filter must NOT be restricted to port 32100 -
the ring is TCP/32002:

    /tool sniffer set filter-ip-address=<device-ip>/32 \\
        filter-stream=yes streaming-enabled=yes streaming-server=<ha-ip>:37008
    /tool sniffer start

This is optional and off by default: it needs a router that can mirror, so it
cannot be a requirement for using the integration.

**Still to confirm:** one capture, one ring. A capture with two rings at known
times would prove both the trigger and the ~22s ``f1 f9`` offset.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from collections.abc import Callable
from typing import NamedTuple

_LOGGER = logging.getLogger(__name__)

MAGIC = 0xF1
MSG_CALL_UNANSWERED = 0xF9
MSG_REGISTER = 0x12
CLOUD_PORT = 32100

#: The device opens a TCP connection here to raise the push notification the
#: instant the button is pressed. Seen on two independent providers, so match
#: on the port rather than on an address.
PUSH_PORT = 32002

TZSP_TAG_END = 0x01
TZSP_TAG_PADDING = 0x00

#: The ring is sent to three servers and retransmitted, so ~9 packets arrive
#: within milliseconds. Collapse them into one event.
RING_DEBOUNCE = 10.0


def strip_tzsp(data: bytes) -> bytes | None:
    """Return the Ethernet frame inside a TZSP packet.

    Header is version(1) type(1) encap(2) followed by a tag list terminated by
    the END tag; other tags carry a length byte.
    """
    if len(data) < 5 or data[0] != 0x01:
        return None
    offset = 4
    while offset < len(data):
        tag = data[offset]
        if tag == TZSP_TAG_END:
            return data[offset + 1 :]
        if tag == TZSP_TAG_PADDING:
            offset += 1
            continue
        if offset + 1 >= len(data):
            return None
        offset += 2 + data[offset + 1]
    return None


class Packet(NamedTuple):
    """A decoded IPv4 packet - UDP or TCP."""

    proto: int
    src: str
    dst: str
    sport: int
    dport: int
    payload: bytes
    tcp_flags: int = 0

    @property
    def is_syn(self) -> bool:
        """A connection being opened, not an established one."""
        return bool(self.tcp_flags & 0x02) and not self.tcp_flags & 0x10


def parse_packet(frame: bytes) -> Packet | None:
    """Decode an Ethernet frame carrying IPv4/UDP or IPv4/TCP."""
    if len(frame) < 14:
        return None
    ethertype = struct.unpack(">H", frame[12:14])[0]
    offset = 14
    if ethertype == 0x8100:  # VLAN
        if len(frame) < 18:
            return None
        ethertype = struct.unpack(">H", frame[16:18])[0]
        offset = 18
    if ethertype != 0x0800 or len(frame) < offset + 20:
        return None
    if (frame[offset] >> 4) != 4:
        return None
    proto = frame[offset + 9]
    if proto not in (socket.IPPROTO_UDP, socket.IPPROTO_TCP):
        return None
    ihl = (frame[offset] & 0x0F) * 4
    src = ".".join(str(b) for b in frame[offset + 12 : offset + 16])
    dst = ".".join(str(b) for b in frame[offset + 16 : offset + 20])
    head = offset + ihl
    if len(frame) < head + 8:
        return None
    sport, dport = struct.unpack(">HH", frame[head : head + 4])

    if proto == socket.IPPROTO_UDP:
        length = struct.unpack(">H", frame[head + 4 : head + 6])[0]
        return Packet(proto, src, dst, sport, dport, frame[head + 8 : head + 8 + max(0, length - 8)])

    if len(frame) < head + 14:
        return None
    data_offset = (frame[head + 12] >> 4) * 4
    return Packet(proto, src, dst, sport, dport, frame[head + data_offset :], frame[head + 13])


class DoorbellListener(asyncio.DatagramProtocol):
    """Listens for mirrored traffic and reports rings."""

    def __init__(
        self,
        device_ip: str | None,
        on_ring: Callable[[], None],
        on_register_port: Callable[[int], None] | None = None,
    ) -> None:
        self._device_ip = device_ip
        self._on_ring = on_ring
        self._on_register_port = on_register_port
        self._transport: asyncio.DatagramTransport | None = None
        self._last_ring = 0.0
        self.rings = 0
        self.unanswered = 0
        self.packets = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets += 1
        frame = strip_tzsp(data)
        if frame is None:
            return
        packet = parse_packet(frame)
        if packet is None:
            return
        if self._device_ip and packet.src != self._device_ip:
            return

        if packet.proto == socket.IPPROTO_TCP:
            self._handle_tcp(packet)
        else:
            self._handle_udp(packet)

    def _handle_tcp(self, packet: Packet) -> None:
        """The ring: a fresh connection to the push service."""
        if packet.dport != PUSH_PORT or not packet.is_syn:
            return
        now = time.monotonic()
        if now - self._last_ring < RING_DEBOUNCE:
            return
        self._last_ring = now
        self.rings += 1
        _LOGGER.debug("Doorbell ring: %s opened a push connection to %s", packet.src, packet.dst)
        try:
            self._on_ring()
        except Exception:  # noqa: BLE001 - never let a listener kill the socket
            _LOGGER.exception("Doorbell callback raised")

    def _handle_udp(self, packet: Packet) -> None:
        if packet.dport != CLOUD_PORT:
            return
        payload = packet.payload
        if len(payload) < 2 or payload[0] != MAGIC:
            return

        if payload[1] == MSG_CALL_UNANSWERED:
            # Observed ~22s after the ring, so almost certainly the call timing
            # out rather than starting. Recorded, deliberately not fired.
            self.unanswered += 1
            _LOGGER.debug("Call-unanswered message from %s", packet.src)
        elif payload[1] == MSG_REGISTER and self._on_register_port is not None:
            # The device registers from the port it will serve sessions on, so
            # this is a free, always-current hint for port discovery.
            try:
                self._on_register_port(packet.sport)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Register-port callback raised", exc_info=True)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Doorbell listener socket error: %s", exc)


async def async_start_listener(
    port: int,
    device_ip: str | None,
    on_ring: Callable[[], None],
    on_register_port: Callable[[int], None] | None = None,
) -> tuple[asyncio.DatagramTransport, DoorbellListener]:
    """Bind the mirror port. Raises OSError if it is already in use."""
    loop = asyncio.get_running_loop()
    listener = DoorbellListener(device_ip, on_ring, on_register_port)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: listener, local_addr=("0.0.0.0", port)
    )
    _LOGGER.info("Doorbell mirror listener bound to UDP %s", port)
    return transport, listener  # type: ignore[return-value]
