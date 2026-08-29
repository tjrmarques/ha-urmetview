#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Standalone network check for the Urmet intercom - one file, stdlib only.

Run this from a machine on the SAME subnet as the intercom. It answers the
questions the integration cannot answer for itself:

  1. Does the device answer a PPPP LAN search?  (unproven - this settles it)
  2. Does it still broadcast its own announcement on UDP 6688?
  3. Does the cloud lookup work, and WHICH UID packing does it accept?
     (the spec and the working prototype disagree; this decides it)
  4. Can the session port be found by scanning locally?

    uv run urmet_check.py
    uv run urmet_check.py --host 10.0.50.6 --uid URMABB-700171-SMCYN
    uv run urmet_check.py --listen          # also wait for the 6688 announce
    uv run urmet_check.py --skip-sweep      # skip the slow part

Nothing here writes to the device or opens a session - it only sends discovery
probes and reads replies.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time

UID_RE = re.compile(r"^([A-Z]{6})-(\d{1,8})-([A-Z]{5})$")

CLOUD_HOSTS = ("p2p1.caycctv.com", "p2p2.caycctv.com", "p2p3.caycctv.com")
CLOUD_FALLBACK = ("3.121.150.135", "15.161.180.1", "35.181.124.200")
CLOUD_PORT = 32100
LAN_SEARCH_PORTS = (32108, 32100, 32106, 32107)
DISCOVERY_PORT = 6688

MSG_NAMES = {
    0x00: "HELLO",
    0x01: "HELLO_ACK",
    0x12: "DEV_LGN_CRC",
    0x13: "DEV_LGN_CRC_ACK",
    0x20: "P2P_REQ",
    0x21: "P2P_REQ_ACK",
    0x30: "LAN_SEARCH",
    0x31: "LAN_NOTIFY",
    0x40: "CANDIDATE",
    0x41: "checkCam",
    0x42: "SESSION_ACK",
    0xE0: "PING",
    0xE1: "PING_ACK",
    0xF9: "CALL_UNANSWERED",
}


def split_uid(uid):
    m = UID_RE.match(uid.strip().upper())
    if not m:
        sys.exit(f"UID {uid!r} must look like URMABB-700171-SMCYN")
    return m.group(1), int(m.group(2)), m.group(3)


def pack_short(uid):
    p, n, s = split_uid(uid)
    return (
        p.encode() + b"\x00\x00\x00" + n.to_bytes(3, "big") + s.encode() + b"\x00" * 3
    )


def pack_long_spec(uid, port):
    """Port at offset 20 - what docs/protocol.md section 2b says."""
    return pack_short(uid) + port.to_bytes(2, "little") + b"\x00" * 14


def pack_long_proto(uid, port):
    """Port at offset 22 - what the working urmet_client.py actually sent."""
    p, n, s = split_uid(uid)
    return (
        p.encode()
        + b"\x00\x00\x00"
        + n.to_bytes(3, "big")
        + s.encode()
        + b"\x00" * 5
        + port.to_bytes(2, "little")
        + b"\x00" * 12
    )


def frame(msg_type, payload=b""):
    return bytes([0xF1, msg_type]) + len(payload).to_bytes(2, "big") + payload


def describe(data):
    if len(data) >= 2 and data[0] == 0xF1:
        return "f1 %02x %s" % (data[1], MSG_NAMES.get(data[1], "?"))
    return "non-PPPP"


def collect(sock, seconds):
    """Gather every datagram that arrives within the window."""
    out = []
    end = time.time() + seconds
    while time.time() < end:
        sock.settimeout(max(0.05, end - time.time()))
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            break
        except OSError:
            break
        out.append((addr, data))
    return out


def local_ip_towards(target):
    """The address of the interface that would route to `target`.

    Deliberately not gethostbyname(gethostname()) - that needs the machine's
    own hostname to resolve, which routinely fails on macOS. A UDP connect()
    sends nothing; it only asks the kernel to pick a route.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 1))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def same_subnet(a, b):
    """Same /24 - a good enough proxy for 'a broadcast can reach it'."""
    if not a or not b:
        return None
    return a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


def udp_socket(broadcast=False, bind_port=0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if broadcast:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", bind_port))
    return s


# --- 1. LAN search ----------------------------------------------------------


def test_lan_search(uid, host, seconds=3.0, same_net=None):
    print("=" * 70)
    print("1. PPPP LAN SEARCH  (confirmed working - checking it still does)")
    print("=" * 70)

    targets = ["255.255.255.255"]
    if host:
        targets.append(host.rsplit(".", 1)[0] + ".255")  # subnet broadcast
        targets.append(host)  # unicast, see note below
    probes = [("f1 30 00 00", frame(0x30)), ("bare 30 00", bytes([0x30, 0x00]))]

    sock = udp_socket(broadcast=True)
    local_port = sock.getsockname()[1]
    print(f"   listening on UDP {local_port}")
    for label, probe in probes:
        for target in targets:
            for port in LAN_SEARCH_PORTS:
                try:
                    sock.sendto(probe, (target, port))
                except OSError as err:
                    print(f"   send to {target}:{port} failed: {err}")
    print(
        f"   sent {len(probes)}x{len(targets)}x{len(LAN_SEARCH_PORTS)} probes, "
        f"waiting {seconds:.0f}s..."
    )
    replies = collect(sock, seconds)
    sock.close()

    if not replies:
        if same_net is False:
            print("\n   RESULT: INCONCLUSIVE - no reply, but this machine is not on")
            print("   the device's subnet, so the broadcast could not have reached it.")
            print("   Re-run from the device's VLAN before drawing any conclusion.")
        else:
            print("\n   RESULT: no reply. The device does not answer LAN search.")
            print("   -> discovery must use the cloud or a port scan.")
        return None
    print()
    best = None
    for (rhost, rport), data in replies:
        print(f"   REPLY from {rhost}:{rport}  {describe(data)}  {data[:24].hex(' ')}")
        if host is None or rhost == host:
            best = (rhost, rport)
    if best:
        print(f"\n   RESULT: LAN SEARCH WORKS. Device answered from port {best[1]}.")
        print("   -> if test 2 confirms it, the cloud is unnecessary.")
    return best


# --- 2. Verify the port with checkCam ---------------------------------------


def test_probe(uid, host, port):
    """Ask a port for a session and see which port actually answers.

    Not a formality. The device sometimes replies to LAN search from a
    short-lived socket, and a session sent to that port gets ICMP
    port-unreachable a few seconds later - which is exactly how this fails in
    Home Assistant: discovery reports success, then the login dies with
    'Connection refused'. What matters is the source port of the reply here.
    """
    print("=" * 70)
    print("2. SESSION PROBE  (does that port actually serve sessions?)")
    print("=" * 70)
    if port is None:
        print("   skipped - no port to probe.")
        return None
    sock = udp_socket()
    sock.sendto(frame(0x41, pack_short(uid)), (host, port))
    print(f"   sent checkCam to {host}:{port}, waiting 2s...")
    replies = collect(sock, 2.0)
    sock.close()

    answered = None
    for (rhost, rport), data in replies:
        if rhost != host or len(data) < 2 or data[0] != 0xF1:
            continue
        print(f"   REPLY from {rhost}:{rport}  {describe(data)}")
        if data[1] in (0x42, 0xE1):
            answered = rport
    if answered is None:
        print("\n   RESULT: no session ack. That port is not serving sessions,")
        print("   even though it answered the broadcast. Fall back to the port scan.")
    elif answered == port:
        print(f"\n   RESULT: port {port} confirmed. Use it directly.")
    else:
        print(f"\n   RESULT: asked {port}, answered from {answered}.")
        print(f"   -> {answered} is the session port. Anything sent to {port} will be")
        print("      refused. This is the mismatch that broke setup in Home Assistant.")
    return answered


# --- 3. Device announcement -------------------------------------------------


def test_broadcast(seconds=20.0):
    print("=" * 70)
    print("3. DEVICE ANNOUNCEMENT on UDP 6688")
    print("=" * 70)
    try:
        sock = udp_socket(broadcast=True, bind_port=DISCOVERY_PORT)
    except OSError as err:
        print(f"   could not bind 6688 ({err}) - something else is using it")
        return
    print(f"   waiting up to {seconds:.0f}s (device announces every ~8s)...")
    got = collect(sock, seconds)
    sock.close()
    for (rhost, _), data in got:
        if len(data) < 330 or data[0:4] != b"\x22\x11\x01\x08":
            continue

        def field(off, ln):
            return data[off : off + ln].split(b"\x00", 1)[0].decode("ascii", "replace")

        print(f"\n   Device at {rhost}")
        print(f"     UID       : {field(108, 24)}")
        print(f"     IP        : {field(84, 9)}")
        print(f"     MAC       : {':'.join('%02x' % b for b in data[100:106])}")
        print(f"     firmware  : {field(232, 11)}")
        print(f"     verify    : {field(132, 20)}")
        print(f"     password  : {field(296, 32)}   <- cleartext, on the wire")
        print("\n   RESULT: device is announcing. Note this carries no session port.")
        return
    print("   RESULT: no announcement seen.")


# --- 4. Cloud lookup, both packings -----------------------------------------


def resolve_cloud():
    servers = []
    for name in CLOUD_HOSTS:
        try:
            for info in socket.getaddrinfo(
                name, CLOUD_PORT, socket.AF_INET, socket.SOCK_DGRAM
            ):
                ip = info[4][0]
                if ip not in servers:
                    servers.append(ip)
                    print(f"   {name} -> {ip}")
        except OSError as err:
            print(f"   {name} DNS FAILED: {err}")
    if not servers:
        print("   using hardcoded IPs")
        servers = list(CLOUD_FALLBACK)
    return servers


def cloud_variant(uid, servers, packer, label, seconds=5.0):
    """One lookup on its own socket, so the answer is attributable."""
    sock = udp_socket()
    local_port = sock.getsockname()[1]
    for server in servers:
        try:
            sock.sendto(frame(0x00), (server, CLOUD_PORT))
        except OSError:
            pass
    time.sleep(0.3)
    payload = packer(uid, local_port)
    for server in servers:
        try:
            sock.sendto(frame(0x20, payload), (server, CLOUD_PORT))
        except OSError:
            pass
    replies = collect(sock, seconds)
    sock.close()

    statuses, candidates = [], []
    for _, data in replies:
        if len(data) < 4 or data[0] != 0xF1:
            continue
        if data[1] == 0x21 and len(data) >= 8:
            statuses.append(data[4])
        elif data[1] == 0x40 and len(data) >= 20:
            port = int.from_bytes(data[6:8], "little")
            ip = ".".join(str(b) for b in reversed(data[8:12]))
            if (ip, port) not in candidates:
                candidates.append((ip, port))

    ok = [s for s in statuses if s == 0]
    bad = [s for s in statuses if s != 0]
    print(f"   {label}:")
    print(
        f"     port at offset {payload.index(local_port.to_bytes(2, 'little'))}, "
        f"{len(replies)} replies, status accepted={len(ok)} rejected={len(bad)}"
        + (f" {[hex(b) for b in bad]}" if bad else "")
    )
    if candidates:
        for ip, port in candidates:
            print(f"     CANDIDATE {ip}:{port}")
    else:
        print("     no candidates")
    return candidates


def test_cloud(uid):
    print("=" * 70)
    print("3. CLOUD LOOKUP - and which UID packing the servers accept")
    print("=" * 70)
    servers = resolve_cloud()
    print()
    spec = cloud_variant(uid, servers, pack_long_spec, "A: spec layout (port @20)")
    proto = cloud_variant(
        uid, servers, pack_long_proto, "B: prototype layout (port @22)"
    )
    # Prefer the RFC1918 candidate: the servers return both a LAN and a public
    # address, and only the LAN one is usable from here.
    for group in (spec, proto):
        group.sort(key=lambda c: not c[0].startswith(("10.", "192.168.", "172.")))
    print()
    if spec and not proto:
        print("   RESULT: the SPEC packing is correct. Drop the prototype variant.")
    elif proto and not spec:
        print("   RESULT: the PROTOTYPE packing is correct. Drop the spec variant.")
    elif spec and proto:
        print("   RESULT: both accepted - either works, keep the cheaper one.")
    else:
        print("   RESULT: neither returned a candidate. Cloud discovery is unavailable")
        print("   (blocked outbound UDP 32100, or the device is not registered).")
    return spec or proto


# --- 5. Port scan -----------------------------------------------------------


def test_sweep(uid, host, rate=3000):
    print("=" * 70)
    print("4. LOCAL PORT SCAN")
    print("=" * 70)
    if not host:
        print("   skipped - needs --host")
        return None
    payload = frame(0x41, pack_short(uid))
    sock = udp_socket()
    print(f"   sending checkCam to {host}:1024-65535 (takes 30-90s; the OS sleep")
    print("   granularity dominates, so it is slower than the raw packet rate)...")
    burst = max(1, rate // 100)
    sent = 0
    start = time.time()
    for port in range(1024, 65536):
        try:
            sock.sendto(payload, (host, port))
        except OSError:
            time.sleep(0.01)
        sent += 1
        if sent % burst == 0:
            time.sleep(0.01)
    print(f"   sent {sent} probes in {time.time() - start:.0f}s, listening 3s...")
    replies = collect(sock, 3.0)
    sock.close()

    found = []
    for (rhost, rport), data in replies:
        if rhost != host or len(data) < 2 or data[0] != 0xF1:
            continue
        if data[1] in (0x42, 0xE1) and rport not in found:
            found.append(rport)
            print(f"   ANSWER from port {rport}: {describe(data)}")
    if found:
        print(f"\n   RESULT: session port is {found[0]}.")
    else:
        print("\n   RESULT: nothing answered. Device off, or a firewall is in the way.")
    return found[0] if found else None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--host", default="10.0.50.6", help="device IP (default 10.0.50.6)")
    ap.add_argument("--uid", default="URMABB-700171-SMCYN")
    ap.add_argument(
        "--listen", action="store_true", help="also wait for the 6688 announce"
    )
    ap.add_argument("--skip-sweep", action="store_true")
    ap.add_argument("--skip-cloud", action="store_true")
    args = ap.parse_args()

    print(f"\nUrmet network check - device {args.host}, UID {args.uid}")
    mine = local_ip_towards(args.host)
    shared = same_subnet(mine, args.host)
    print(f"Running from {mine or 'unknown address'}")
    if shared is False:
        print(f"  WARNING: {mine} is not on the same /24 as {args.host}. A broadcast")
        print("  cannot cross subnets, so test 1 will fail regardless of whether the")
        print("  device supports LAN search. Move this machine onto the device's VLAN")
        print("  for that result to mean anything.")
    elif shared:
        print("  Same subnet as the device - test 1 is meaningful.")
    print()

    summary = {}
    lan = test_lan_search(args.uid, args.host, same_net=shared)
    summary["lan_search"] = lan
    print()
    summary["probe"] = test_probe(args.uid, args.host, lan[1] if lan else None)
    print()
    if args.listen:
        test_broadcast()
        print()
    if not args.skip_cloud:
        summary["cloud"] = test_cloud(args.uid)
        print()
    if not args.skip_sweep:
        summary["sweep"] = test_sweep(args.uid, args.host)
        print()

    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    lan = summary.get("lan_search")
    probe = summary.get("probe")
    if lan:
        lan_text = "WORKS, port %s" % lan[1]
    elif shared is False:
        lan_text = "inconclusive (wrong subnet)"
    else:
        lan_text = "no reply"
    print(f"  LAN search : {lan_text}")
    if lan and probe is None:
        probe_text = "port %s answered the broadcast but serves no session" % lan[1]
    elif probe and lan and probe != lan[1]:
        probe_text = "answered from %s, NOT %s - use %s" % (probe, lan[1], probe)
    elif probe:
        probe_text = "port %s confirmed" % probe
    else:
        probe_text = "not run"
    print(f"  Probe      : {probe_text}")
    cloud = summary.get("cloud")
    if "cloud" not in summary:
        cloud_text = "skipped"
    else:
        cloud_text = "works -> %s" % (cloud[0],) if cloud else "no candidates"
    print(f"  Cloud      : {cloud_text}")
    sweep = summary.get("sweep")
    if "sweep" not in summary:
        sweep_text = "skipped"
    else:
        sweep_text = "port %s" % sweep if sweep else "nothing found"
    print(f"  Port scan  : {sweep_text}")
    print()
    if probe:
        print(f"  -> Fully local discovery works; session port {probe}.")
    elif lan:
        print("  -> LAN search answers but the session probe did not. The port scan")
        print("     result below is the one to trust.")
    elif sweep:
        print(
            f"  -> Use Host {args.host} with Session port {sweep} in the config flow,"
        )
        print("     and you can switch the cloud option off.")
    elif cloud:
        print("  -> Cloud discovery is the only working method; leave that option on.")
    else:
        print("  -> Nothing worked. Check the device is powered and reachable:")
        print(f"     ping {args.host}")
    print()


if __name__ == "__main__":
    main()
