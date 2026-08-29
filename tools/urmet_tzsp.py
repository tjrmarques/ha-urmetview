#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Local doorbell trigger, by mirroring the device's cloud traffic.

The ring is a cloud push to registered smartphones - nothing is sent on the LAN,
so Home Assistant cannot receive it directly. But the device opens a TCP
connection to port 32002 on its push servers the instant the button is pressed,
and a router can mirror that to us. That turns an unreachable cloud push into a
local event.

On the MikroTik, as a firewall rule (persists across reboots, one packet per
ring):

    /ip firewall mangle
    add chain=prerouting action=sniff-tzsp \\
        protocol=tcp dst-port=32002 src-address=<device-ip> \\
        connection-state=new \\
        sniff-target=<this-host> sniff-target-port=37008

To watch everything instead while investigating, widen it by dropping the
protocol/port/connection-state matchers - but expect video to come with it.

Then here:

    uv run tools/urmet_tzsp.py --device-ip 10.0.50.6

The ring is a TCP connection to port 32002 on the push servers, made the
instant the button is pressed. Note the sniffer filter must NOT be limited to
port 32100, or you will miss it.

Two decoys, both seen once in the same capture: `f1 f9` arrives 22s later (the
call going unanswered) and `f1 12` every ~33s (registration).
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import struct
import time

TZSP_PORT = 37008
TZSP_TAG_END = 0x01
TZSP_TAG_PADDING = 0x00

MSG_NAMES = {
    0x00: "HELLO",
    0x01: "HELLO_ACK",
    0x10: "DEV_LGN",
    0x11: "DEV_LGN_ACK",
    0x12: "DEV_LGN_CRC (periodic, ~33s)",
    0x13: "DEV_LGN_CRC_ACK",
    0x20: "P2P_REQ (lookup)",
    0x21: "P2P_REQ_ACK",
    0x30: "LAN_SEARCH",
    0x40: "PUNCH_TO",
    0x41: "PUNCH_PKT / checkCam",
    0x42: "P2P_RDY / session ack",
    0xD0: "DRW (data)",
    0xD1: "DRW_ACK",
    0xE0: "ALIVE (ping)",
    0xE1: "ALIVE_ACK",
    0xF0: "CLOSE",
    0xF9: "call unanswered (~22s after ring)",
}

PUSH_PORT = 32002

#: Sent once, ~22s after the button press - the call timing out, not starting.
MSG_UNANSWERED = 0xF9

#: Periodic device registration, roughly every 33s. It looks event-shaped in a
#: short capture, which is exactly why it must NOT be used as a ring trigger -
#: it would fire the doorbell twice a minute, forever.
MSG_REGISTER = 0x12


def strip_tzsp(data: bytes) -> bytes | None:
    """Remove the TZSP header, returning the encapsulated Ethernet frame.

    Header is version(1) type(1) encap(2), then a tag list terminated by the
    END tag. Tags other than END/PADDING carry a length byte.
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


def parse_udp(frame: bytes):
    """Pull addresses, payload, protocol and TCP flags out of a frame."""
    if len(frame) < 14:
        return None
    ethertype = struct.unpack(">H", frame[12:14])[0]
    offset = 14
    if ethertype == 0x8100:  # VLAN tag
        if len(frame) < 18:
            return None
        ethertype = struct.unpack(">H", frame[16:18])[0]
        offset = 18
    if ethertype != 0x0800 or len(frame) < offset + 20:
        return None
    proto = frame[offset + 9]
    if (frame[offset] >> 4) != 4 or proto not in (socket.IPPROTO_UDP, socket.IPPROTO_TCP):
        return None
    ihl = (frame[offset] & 0x0F) * 4
    src = ".".join(str(b) for b in frame[offset + 12 : offset + 16])
    dst = ".".join(str(b) for b in frame[offset + 16 : offset + 20])
    head = offset + ihl
    if len(frame) < head + 14:
        return None
    sport, dport = struct.unpack(">HH", frame[head : head + 4])
    if proto == socket.IPPROTO_UDP:
        length = struct.unpack(">H", frame[head + 4 : head + 6])[0]
        payload = frame[head + 8 : head + 8 + max(0, length - 8)]
        return src, sport, dst, dport, payload, proto, 0
    doff = (frame[head + 12] >> 4) * 4
    return src, sport, dst, dport, frame[head + doff :], proto, frame[head + 13]


class Sniffer(asyncio.DatagramProtocol):
    def __init__(self, device_ip: str | None, quiet: bool) -> None:
        self.device_ip = device_ip
        self.quiet = quiet
        self.start = time.monotonic()
        self.counts: dict[int, int] = {}
        self.rings = 0
        self.last_register_port: int | None = None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        frame = strip_tzsp(data)
        if frame is None:
            return
        parsed = parse_udp(frame)
        if parsed is None:
            return
        src, sport, dst, dport, payload, proto, flags = parsed
        if self.device_ip and self.device_ip not in (src, dst):
            return

        if proto == socket.IPPROTO_TCP:
            # The ring: a fresh connection to the push service.
            if dport == PUSH_PORT and flags & 0x02 and not flags & 0x10:
                self.rings += 1
                print(
                    "\n" + "=" * 62
                    + f"\n  DOORBELL RING #{self.rings}  ({time.strftime('%H:%M:%S')})"
                    + f"\n  {src} -> {dst}:{dport}\n"
                    + "=" * 62 + "\n",
                    flush=True,
                )
            return

        if len(payload) < 2 or payload[0] != 0xF1:
            return

        msg_type = payload[1]
        self.counts[msg_type] = self.counts.get(msg_type, 0) + 1
        if self.quiet and msg_type in (0xE0, 0xE1, 0xD0, 0xD1):
            return
        name = MSG_NAMES.get(msg_type, "?")
        elapsed = time.monotonic() - self.start
        print(
            f"{elapsed:8.3f}  {src}:{sport} -> {dst}:{dport}  "
            f"f1 {msg_type:02x} {name:<28} len={len(payload)}  {payload[:24].hex(' ')}",
            flush=True,
        )

        if msg_type == MSG_REGISTER:
            # The registration source port is the port the device will serve a
            # session on - useful for port discovery, not for ring detection.
            self.last_register_port = sport


async def _run(args: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    sniffer = Sniffer(args.device_ip, quiet=not args.show_all)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: sniffer, local_addr=("0.0.0.0", args.port)
    )
    print(f"Listening for TZSP on UDP {args.port}.")
    if args.device_ip:
        print(f"Filtering to traffic involving {args.device_ip}.")
    print("Ring the bell and watch. Ctrl-C to stop.\n")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        transport.close()

    print("\nMessage type totals:")
    for msg_type, count in sorted(sniffer.counts.items()):
        print(f"  f1 {msg_type:02x} {MSG_NAMES.get(msg_type,'?'):<28} {count}")
    print(f"\nRings detected: {sniffer.rings}")
    if sniffer.last_register_port:
        print(f"Last registration source port (= likely session port): {sniffer.last_register_port}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=TZSP_PORT)
    parser.add_argument("--device-ip", help="only show traffic involving this IP")
    parser.add_argument("--show-all", action="store_true", help="include pings and data packets")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
