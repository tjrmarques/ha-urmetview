"""Doorbell detection by mirroring the device's cloud-bound traffic.

When the bell rings the device sends **nothing on the LAN** - the alert is a
cloud push addressed to registered smartphones, which Home Assistant cannot
receive. What the device *does* do is announce the ring to Urmet's rendezvous
servers as an ``f1 f9`` message. Mirroring that one packet to us turns an
unreachable cloud push into a local event.

Confirmed against a 109-second capture containing exactly one ring:

* ``f1 f9`` appeared once, to all three cloud servers, and never again.
* ``f1 12`` fired every ~33 seconds throughout - it is periodic registration,
  and using it as the trigger would ring the doorbell twice a minute forever.

Set up on a MikroTik, filtered so only control traffic is mirrored:

    /tool sniffer set filter-ip-address=<device-ip>/32 filter-port=32100 \\
        filter-stream=yes streaming-enabled=yes streaming-server=<ha-ip>:37008
    /tool sniffer start

This is optional and off by default: it needs a router that can mirror, so it
cannot be a requirement for using the integration.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

MAGIC = 0xF1
MSG_RING = 0xF9
MSG_REGISTER = 0x12
CLOUD_PORT = 32100

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


def parse_udp(frame: bytes) -> tuple[str, int, str, int, bytes] | None:
    """Extract addresses and payload from an Ethernet frame carrying IPv4/UDP."""
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
    if (frame[offset] >> 4) != 4 or frame[offset + 9] != socket.IPPROTO_UDP:
        return None
    ihl = (frame[offset] & 0x0F) * 4
    src = ".".join(str(b) for b in frame[offset + 12 : offset + 16])
    dst = ".".join(str(b) for b in frame[offset + 16 : offset + 20])
    udp = offset + ihl
    if len(frame) < udp + 8:
        return None
    sport, dport, length = struct.unpack(">HHH", frame[udp : udp + 6])
    return src, sport, dst, dport, frame[udp + 8 : udp + 8 + max(0, length - 8)]


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
        self.packets = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets += 1
        frame = strip_tzsp(data)
        if frame is None:
            return
        parsed = parse_udp(frame)
        if parsed is None:
            return
        src, sport, _dst, dport, payload = parsed

        if self._device_ip and src != self._device_ip:
            return
        if dport != CLOUD_PORT:
            return
        if len(payload) < 2 or payload[0] != MAGIC:
            return

        if payload[1] == MSG_RING:
            now = time.monotonic()
            if now - self._last_ring < RING_DEBOUNCE:
                return
            self._last_ring = now
            self.rings += 1
            _LOGGER.debug("Doorbell ring detected from %s", src)
            try:
                self._on_ring()
            except Exception:  # noqa: BLE001 - never let a listener kill the socket
                _LOGGER.exception("Doorbell callback raised")
        elif payload[1] == MSG_REGISTER and self._on_register_port is not None:
            # The device registers from the port it will serve sessions on, so
            # this is a free, always-current hint for port discovery.
            try:
                self._on_register_port(sport)
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
