#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pull the login auth hash (and other useful fields) out of a packet capture.

The auth hash cannot be derived from the device password - the derivation was
tested extensively and never reproduced - so it has to be lifted once from a
real app login and then stored as the credential. This finds it.

    uv run tools/urmet_pcap.py capture.pcap

Capture a login by starting the sniffer, then opening the UrmetView app.

Also prints a traffic summary, which is the fastest way to sanity-check a
capture before analysing it: whether the session is present at all, whether
media is flowing, and whether the capture was truncated by a size limit.
"""

from __future__ import annotations

import argparse
import collections
import re
import socket
import struct
import sys

AUTH_RE = re.compile(rb'\{"username":"([ -~]{1,32})","auth":"([0-9A-Fa-f]{32})"\}')
JSON_RE = re.compile(rb'\{"[ -~]{2,300}?\}')

MSG_NAMES = {
    0x00: "HELLO", 0x01: "HELLO_ACK", 0x12: "DEV_LGN_CRC (periodic)",
    0x13: "DEV_LGN_CRC_ACK",
    0x20: "P2P_REQ", 0x21: "P2P_REQ_ACK", 0x30: "LAN_SEARCH", 0x31: "LAN_NOTIFY",
    0x41: "checkCam", 0x42: "session ack", 0xD0: "DATA", 0xD1: "ACK",
    0xE0: "ping", 0xE1: "ping ack", 0xF0: "CLOSE", 0xF9: "DOORBELL RING",
}

MSG_RING = 0xF9


def read_pcap(path: str):
    """Yield (timestamp, link-layer frame) from a classic pcap file."""
    with open(path, "rb") as handle:
        header = handle.read(24)
        if len(header) < 24:
            raise ValueError("file too short to be a pcap")
        magic = header[:4]
        if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
            endian = "<"
        elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
            endian = ">"
        elif magic[:4] == b"\x0a\x0d\x0d\x0a":
            raise ValueError("this is a pcapng file; save as classic pcap instead")
        else:
            raise ValueError(f"not a pcap file (magic {magic.hex()})")
        while True:
            record = handle.read(16)
            if len(record) < 16:
                return
            sec, usec, caplen, _ = struct.unpack(endian + "IIII", record)
            data = handle.read(caplen)
            if len(data) < caplen:
                return
            yield sec + usec / 1e6, data


def parse_udp(frame: bytes):
    if len(frame) < 14:
        return None
    ethertype = struct.unpack(">H", frame[12:14])[0]
    offset = 14
    if ethertype == 0x8100:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pcap")
    parser.add_argument("--json", action="store_true", help="dump every JSON payload seen")
    args = parser.parse_args()

    try:
        packets = list(read_pcap(args.pcap))
    except (OSError, ValueError) as err:
        print(f"Could not read {args.pcap}: {err}", file=sys.stderr)
        return 1

    if not packets:
        print("Capture is empty.", file=sys.stderr)
        return 1

    flows: collections.Counter = collections.Counter()
    types: collections.Counter = collections.Counter()
    credentials: set[tuple[str, str]] = set()
    jsons: list[str] = []
    raw = b"".join(frame for _, frame in packets)

    for match in AUTH_RE.finditer(raw):
        credentials.add((match.group(1).decode(), match.group(2).decode()))

    t0 = packets[0][0]
    for _, frame in packets:
        parsed = parse_udp(frame)
        if parsed is None:
            continue
        src, sport, dst, dport, payload = parsed
        flows[(src, dst, dport)] += 1
        if len(payload) >= 2 and payload[0] == 0xF1:
            types[payload[1]] += 1
        if args.json:
            for match in JSON_RE.finditer(payload):
                text = match.group().decode("ascii", "replace")
                if text not in jsons:
                    jsons.append(text)

    rings: list[float] = []
    for ts, frame in packets:
        parsed = parse_udp(frame)
        if parsed is None:
            continue
        payload = parsed[4]
        if len(payload) >= 2 and payload[0] == 0xF1 and payload[1] == MSG_RING:
            if not rings or ts - rings[-1] > 1.0:
                rings.append(ts)

    duration = packets[-1][0] - t0
    size = sum(len(f) for _, f in packets)
    print(f"{args.pcap}: {len(packets)} packets, {size/1e6:.2f} MB, {duration:.1f}s")
    if duration > 0 and size / duration > 150_000:
        print("  NOTE: high data rate - if this capture looks short, it hit a size limit")

    print("\nTop flows:")
    for (src, dst, dport), count in flows.most_common(10):
        print(f"  {src:<16} -> {dst:<16}:{dport:<6} {count}")

    print("\nUrmet message types:")
    for msg_type, count in sorted(types.items()):
        print(f"  f1 {msg_type:02x} {MSG_NAMES.get(msg_type,'?'):<24} {count}")

    if rings:
        print(f"\n  *** {len(rings)} DOORBELL RING(S) ***")
        for when in rings:
            print(f"      at +{when - t0:.3f}s into the capture")

    print()
    if credentials:
        for username, auth in credentials:
            print("=" * 60)
            print(f"  FOUND CREDENTIALS   username: {username}")
            print(f"                      auth:     {auth}")
            print("=" * 60)
            print("  Pass this to the tools as --auth, and store it as the")
            print("  integration's secret. Treat it exactly like a password:")
            print("  it is static and replayable for gate/lock control.")
    else:
        print("No login exchange in this capture.")
        print("Capture again while opening the app - the login is sent once, at connect.")

    if args.json:
        print("\nJSON payloads:")
        for text in jsons:
            print(f"  {text[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
