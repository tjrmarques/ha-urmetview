#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Why is LAN search not answering? One question per test, no shotgun.

The main check sprays every probe at every target at every port and waits, so
a silence tells you nothing about *which* combination failed. This sends one
thing at a time and, crucially, uses a **connected** UDP socket for unicast so
ICMP port-unreachable surfaces as an error instead of vanishing.

That is the distinction that matters:

  refused   nothing is bound to that port on the device - LAN search is not
            available right now, and no amount of retrying will help
  silence   something is bound and chose not to answer, or the packet never
            arrived (on WiFi, broadcast is the usual suspect)
  reply     works - and the source port tells us where the session should go

    uv run tools/urmet_lansearch.py --host 10.0.50.6
    uv run tools/urmet_lansearch.py --host 10.0.50.6 --also 10169,20116,23691
"""

from __future__ import annotations

import argparse
import socket
import struct
import time

LAN_SEARCH_PORTS = (32108, 32100, 32106, 32107)
MAGIC = 0xF1
TYPES = {
    0x30: "LAN_SEARCH", 0x31: "LAN_NOTIFY", 0x41: "PUNCH/checkCam",
    0x42: "SESSION_ACK", 0xE0: "PING", 0xE1: "PING_ACK",
    0x12: "DEV_LOGIN", 0x13: "DEV_LOGIN_ACK",
}


def frame(msg_type: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BBH", MAGIC, msg_type, len(payload)) + payload


def describe(data: bytes) -> str:
    if len(data) < 2 or data[0] != MAGIC:
        return f"non-PPPP, {len(data)} bytes"
    return f"f1 {data[1]:02x} {TYPES.get(data[1], 'unknown')}"


def local_ip_towards(target: str) -> str | None:
    """Route lookup without DNS - gethostbyname(gethostname()) fails on macOS."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def probe_unicast(host: str, port: int, payload: bytes, wait: float = 1.2) -> str:
    """Connected socket, so the kernel reports ICMP port-unreachable to us."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(wait)
    try:
        sock.connect((host, port))
        sock.send(payload)
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                data = sock.recv(2048)
            except ConnectionRefusedError:
                return "REFUSED  (ICMP port-unreachable - nothing is bound there)"
            except socket.timeout:
                return "silence  (bound but not answering, or packet lost)"
            except OSError as err:
                return f"error    ({err})"
            # A connected socket only accepts replies from the same port, so a
            # device answering from elsewhere looks like silence here. That is
            # the point of the unconnected pass below.
            return f"REPLY    {describe(data)}  from port {port}"
        return "silence  (bound but not answering, or packet lost)"
    finally:
        sock.close()


def probe_unconnected(host: str, port: int, payload: bytes, wait: float = 1.5) -> str:
    """Unconnected, so a reply from a *different* source port is still seen."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(wait)
    try:
        sock.sendto(payload, (host, port))
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                data, (rhost, rport) = sock.recvfrom(2048)
            except ConnectionRefusedError:
                return "REFUSED  (ICMP port-unreachable)"
            except socket.timeout:
                return "silence"
            except OSError as err:
                return f"error    ({err})"
            if rhost != host:
                continue
            same = "same port" if rport == port else f"DIFFERENT port {rport}"
            return f"REPLY    {describe(data)}  from {same}"
        return "silence"
    finally:
        sock.close()


def probe_broadcast(addr: str, port: int, payload: bytes, wait: float = 2.0) -> list:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)
    out = []
    try:
        sock.sendto(payload, (addr, port))
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                data, (rhost, rport) = sock.recvfrom(2048)
            except (socket.timeout, ConnectionRefusedError):
                continue
            except OSError:
                break
            out.append((rhost, rport, describe(data)))
    finally:
        sock.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--host", required=True, help="device IP, e.g. 10.0.50.6")
    ap.add_argument(
        "--also",
        default="",
        help="extra ports to try LAN search on, e.g. the ones the scan found",
    )
    ap.add_argument("--rounds", type=int, default=8, help="repeat, to catch a flaky responder")
    args = ap.parse_args()

    mine = local_ip_towards(args.host)
    print(f"\nLAN search diagnosis - device {args.host}, from {mine or 'unknown'}")
    if mine and mine.rsplit(".", 1)[0] != args.host.rsplit(".", 1)[0]:
        print(f"  WARNING: {mine} is not on the device's /24. Broadcast cannot cross,")
        print("  so only the unicast results below will mean anything.")
    print()

    probe = frame(0x30)
    extra = [int(x) for x in args.also.replace(",", " ").split() if x.strip().isdigit()]

    print("=" * 70)
    print("1. UNICAST LAN SEARCH, connected socket (shows ICMP)")
    print("=" * 70)
    print("   'REFUSED' means the port is closed. That is a real answer, not a")
    print("   failure - it tells us the device is not offering LAN search.\n")
    for port in LAN_SEARCH_PORTS:
        print(f"   {args.host}:{port:<6} {probe_unicast(args.host, port, probe)}")

    print()
    print("=" * 70)
    print("2. UNICAST LAN SEARCH, unconnected (catches a reply from another port)")
    print("=" * 70)
    for port in LAN_SEARCH_PORTS:
        print(f"   {args.host}:{port:<6} {probe_unconnected(args.host, port, probe)}")

    if extra:
        print()
        print("=" * 70)
        print("3. LAN SEARCH ON THE PORTS THAT DID ANSWER checkCam")
        print("=" * 70)
        print("   If the device answers 0x30 here but not on 32108, LAN search")
        print("   lives on the session sockets and 32108 is a red herring.\n")
        for port in extra:
            print(f"   {args.host}:{port:<6} {probe_unconnected(args.host, port, probe)}")
            print(f"   {args.host}:{port:<6} checkCam -> "
                  f"{probe_unconnected(args.host, port, frame(0x41))}")

    print()
    print("=" * 70)
    print("4. BROADCAST, one target at a time")
    print("=" * 70)
    subnet = args.host.rsplit(".", 1)[0] + ".255"
    for addr in ("255.255.255.255", subnet):
        for port in (32108, 32100):
            replies = probe_broadcast(addr, port, probe)
            if replies:
                for rhost, rport, what in replies:
                    print(f"   {addr}:{port:<6} REPLY from {rhost}:{rport}  {what}")
            else:
                print(f"   {addr}:{port:<6} silence")

    print()
    print("=" * 70)
    print(f"5. REPEATABILITY - {args.rounds} rounds on 32108, 5s apart")
    print("=" * 70)
    print("   A responder that comes and goes looks like this. A dead one does not.\n")
    for round_no in range(1, args.rounds + 1):
        uni = probe_unconnected(args.host, 32108, probe, wait=1.0)
        bcast = probe_broadcast("255.255.255.255", 32108, probe, wait=1.0)
        print(f"   round {round_no}: unicast {uni:<52} broadcast "
              f"{len(bcast)} repl{'y' if len(bcast) == 1 else 'ies'}")
        if round_no < args.rounds:
            time.sleep(5.0)

    print()
    print("=" * 70)
    print("WHAT TO CONCLUDE")
    print("=" * 70)
    print("  REFUSED on 32108           -> the device is not listening there at all.")
    print("                                LAN search is unavailable; the earlier")
    print("                                success must have come from a state the")
    print("                                device is no longer in (a reboot, or the")
    print("                                app having been open recently).")
    print("  silence unicast, no ICMP   -> something IS bound to 32108 and is")
    print("                                ignoring us. Worth trying the other probe")
    print("                                encodings and the session ports.")
    print("  reply on a session port    -> LAN search is served there, not on 32108,")
    print("                                and the integration should ask there.")
    print("  unicast works, broadcast   -> your AP is dropping broadcast to wireless")
    print("  does not                      clients. Nothing to do with the device.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
