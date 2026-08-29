#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Does a login actually work? Single file, nothing to install.

The other scripts prove the device is *findable*. This proves it is *usable*,
which is the only question that matters for the integration - discovery has
reported success and then had the login die with ECONNREFUSED several times
now, so "a port answered" and "a session works" have to be measured
separately.

Self-contained on purpose: the repo's tools/ import the integration's protocol
package, which is no use on a laptop that has not got the repo.

    # everything: LAN search, cloud, sweep - then log in to each and compare
    uv run urmet_login.py

    # local only, and skip the 30-60s scan
    uv run urmet_login.py --no-cloud --no-sweep

    # pin a port, no discovery at all
    uv run urmet_login.py --host 10.0.50.6 --port 10169

The UID and hash default to the captured ones; --auth overrides.

It tries *every* candidate rather than stopping at the first success, because
the useful output is which discovery method yields a port that actually logs
in - not merely that one of them does.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import time

DEFAULT_UID = "URMABB-700171-SMCYN"
DEFAULT_AUTH = "A8935C8DA4ABAD9782B7045054680D67"

MAGIC = 0xF1
MSG_CHECKCAM = 0x41
MSG_SESSION_ACK = 0x42
MSG_DATA = 0xD0
MSG_ACK = 0xD1
MSG_PING = 0xE0
MSG_PING_ACK = 0xE1
MSG_LAN_SEARCH = 0x30
MSG_CLOUD_HELLO = 0x00
MSG_CLOUD_LOOKUP = 0x20
MSG_CLOUD_STATUS = 0x21
MSG_CLOUD_CANDIDATE = 0x40

CLOUD_HOSTS = ("p2p1.caycctv.com", "p2p2.caycctv.com", "p2p3.caycctv.com")
CLOUD_PORT = 32100

MARKER_CMD = b"\xa3\x01\x00\xff"
CHANNEL_COMMAND = 0x00

SUBCMD_HELLO = 0x00C8
SUBCMD_LOGIN = 0x000B
SUBCMD_VIDEO_STOP = 0x0065
SUBCMD_AUDIO_STOP = 0x0067
SUBCMD_TALK_ACTION = 0x0069
SUBCMD_TALK_CHANNEL_OFF = 0x006A

PING_INTERVAL = 1.2
LOGIN_TIMEOUT = 8.0

TYPES = {
    0x30: "LAN_SEARCH",
    0x31: "LAN_NOTIFY",
    0x41: "checkCam",
    0x42: "SESSION_ACK",
    0xD0: "DRW",
    0xD1: "DRW_ACK",
    0xE0: "PING",
    0xE1: "PING_ACK",
}


# --- framing ----------------------------------------------------------------


def simple(msg_type: int, payload: bytes = b"") -> bytes:
    return bytes([MAGIC, msg_type]) + len(payload).to_bytes(2, "big") + payload


def pack_uid(uid: str) -> bytes:
    """20 bytes. The numeric middle is a 3-byte big-endian int, not ASCII."""
    prefix, number, suffix = uid.split("-")
    return (
        prefix.encode()
        + b"\x00" * 3
        + int(number).to_bytes(3, "big")
        + suffix.encode()
        + b"\x00" * 3
    )


def data_frame(channel: int, seq: int, payload: bytes) -> bytes:
    """The length field counts the 4-byte d1 prefix as well as the payload."""
    body = bytes([MSG_ACK, channel]) + (seq & 0xFFFF).to_bytes(2, "big") + payload
    return bytes([MAGIC, MSG_DATA]) + len(body).to_bytes(2, "big") + body


def ack_frame(channel: int, seqs: list[int]) -> bytes:
    body = bytes([MSG_ACK, channel, 0x00, len(seqs)])
    for seq in seqs:
        body += (seq & 0xFFFF).to_bytes(2, "big")
    return bytes([MAGIC, MSG_ACK]) + len(body).to_bytes(2, "big") + body


def command_block(subcmd: int, text: str, seq: int) -> bytes:
    """The length includes the trailing NUL. Subcmd and seq are little-endian."""
    body = text.encode("utf-8") + b"\x00"
    return (
        MARKER_CMD
        + (subcmd & 0xFFFF).to_bytes(2, "little")
        + (seq & 0xFFFF).to_bytes(2, "little")
        + b"\x00\x00\x00\x00"
        + len(body).to_bytes(4, "little")
        + body
    )


def parse_data(packet: bytes) -> tuple[int, int, bytes] | None:
    if len(packet) < 8 or packet[0] != MAGIC or packet[1] != MSG_DATA:
        return None
    return packet[5], struct.unpack(">H", packet[6:8])[0], packet[8:]


def parse_blocks(buffer: bytes) -> tuple[list[tuple[int, str]], bytes]:
    out: list[tuple[int, str]] = []
    offset = 0
    while len(buffer) - offset >= 16:
        if buffer[offset : offset + 4] != MARKER_CMD:
            nxt = buffer.find(MARKER_CMD, offset + 1)
            if nxt == -1:
                return out, b""
            offset = nxt
            continue
        subcmd = int.from_bytes(buffer[offset + 4 : offset + 6], "little")
        length = int.from_bytes(buffer[offset + 12 : offset + 16], "little")
        end = offset + 16 + length
        if length > (1 << 20) or end > len(buffer):
            break
        text = buffer[offset + 16 : end].split(b"\x00", 1)[0].decode("utf-8", "replace")
        out.append((subcmd, text))
        offset = end
    return out, buffer[offset:]


# --- discovery --------------------------------------------------------------


def lan_search(wait: float = 2.5) -> list[tuple[str, int]]:
    """One valid probe, broadcast. Replies come from the device's own port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))
    found: list[tuple[str, int]] = []
    try:
        sock.sendto(simple(MSG_LAN_SEARCH), ("255.255.255.255", 32108))
        end = time.time() + wait
        while time.time() < end:
            sock.settimeout(max(0.05, end - time.time()))
            try:
                packet, addr = sock.recvfrom(2048)
            except (TimeoutError, OSError):
                break
            if len(packet) >= 2 and packet[0] == MAGIC and addr not in found:
                found.append(addr)
    finally:
        sock.close()
    return found


def probe(host: str, port: int, uid: str, wait: float = 1.5) -> int | None:
    """Return the port the session ack came *from*, which is not always `port`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    try:
        sock.sendto(simple(MSG_CHECKCAM, pack_uid(uid)), (host, port))
        end = time.time() + wait
        while time.time() < end:
            sock.settimeout(max(0.05, end - time.time()))
            try:
                packet, (rhost, rport) = sock.recvfrom(2048)
            except (TimeoutError, OSError):
                return None
            if rhost == host and len(packet) >= 2 and packet[0] == MAGIC:
                if packet[1] in (MSG_SESSION_ACK, MSG_PING_ACK):
                    return rport
    finally:
        sock.close()
    return None


def cloud_lookup(uid: str, wait: float = 4.0) -> list[tuple[str, int]]:
    """Ask Urmet's rendezvous servers where the device is.

    The servers answer with both a LAN and a public/relay address; only the
    LAN one is usable from here, so private addresses sort first.
    """
    servers: list[str] = []
    for hostname in CLOUD_HOSTS:
        try:
            for info in socket.getaddrinfo(
                hostname, CLOUD_PORT, proto=socket.IPPROTO_UDP
            ):
                servers.append(info[4][0])
        except OSError:
            continue
    if not servers:
        print("   DNS failed for all three rendezvous servers")
        return []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    local_port = sock.getsockname()[1]
    found: list[tuple[str, int]] = []
    try:
        for server in servers:
            try:
                sock.sendto(simple(MSG_CLOUD_HELLO), (server, CLOUD_PORT))
            except OSError:
                pass
        time.sleep(0.3)
        # 36-byte form with our local port at offset 20, per the spec.
        payload = pack_uid(uid) + local_port.to_bytes(2, "little") + b"\x00" * 14
        for server in servers:
            try:
                sock.sendto(simple(MSG_CLOUD_LOOKUP, payload), (server, CLOUD_PORT))
            except OSError:
                pass
        end = time.time() + wait
        while time.time() < end:
            sock.settimeout(max(0.05, end - time.time()))
            try:
                packet = sock.recv(2048)
            except (TimeoutError, OSError):
                break
            if len(packet) < 4 or packet[0] != MAGIC:
                continue
            if packet[1] == MSG_CLOUD_STATUS and len(packet) >= 8:
                if packet[4] != 0:
                    print(f"   cloud rejected the lookup (status 0x{packet[4]:02x})")
                continue
            if packet[1] != MSG_CLOUD_CANDIDATE or len(packet) < 20:
                continue
            port = int.from_bytes(packet[6:8], "little")
            host = ".".join(str(b) for b in reversed(packet[8:12]))
            if (host, port) not in found:
                found.append((host, port))
    finally:
        sock.close()
    found.sort(key=lambda c: not c[0].startswith(("10.", "192.168.", "172.")))
    return found


def sweep(host: str, uid: str, rate: int = 20000) -> list[int]:
    """checkCam every port and return all that answer, ascending.

    All of them, not the first: the device keeps several sockets open and they
    do not all serve sessions, which is the whole point of this exercise.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    payload = simple(MSG_CHECKCAM, pack_uid(uid))
    started = time.time()
    answered: set[int] = set()
    try:
        sent = 0
        for port in range(1024, 65536):
            try:
                sock.sendto(payload, (host, port))
            except OSError:
                time.sleep(0.002)
            sent += 1
            if sent % 256 == 0:
                behind = started + sent / rate - time.time()
                if behind > 0:
                    time.sleep(behind)
        print(f"   swept {sent} ports in {time.time() - started:.0f}s, listening 3s...")
        end = time.time() + 3.0
        while time.time() < end:
            sock.settimeout(max(0.05, end - time.time()))
            try:
                packet, (rhost, rport) = sock.recvfrom(2048)
            except (TimeoutError, OSError):
                break
            if rhost == host and len(packet) >= 2 and packet[0] == MAGIC:
                if packet[1] in (MSG_SESSION_ACK, MSG_PING_ACK):
                    answered.add(rport)
    finally:
        sock.close()
    return sorted(answered)


# --- the actual login -------------------------------------------------------


class Session:
    """Just enough session to log in, ack, ping and tear down cleanly."""

    def __init__(
        self,
        host: str,
        port: int,
        uid: str,
        auth: str,
        username: str,
        sock: socket.socket | None = None,
    ):
        """``sock`` adopts an existing socket instead of opening a new one.

        That matters for the punch experiment: the device may bind the session
        it offers to the peer tuple it punched at, in which case only the
        socket that received the punch can use it. A fresh socket would be a
        different peer as far as the device is concerned.
        """
        self.host, self.port = host, port
        self.uid, self.auth, self.username = uid, auth, username
        self.adopted = sock is not None
        if sock is not None:
            self.sock = sock
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Connected, so ICMP port-unreachable reaches us as an exception
            # instead of silently vanishing - that is the whole failure mode
            # here. An adopted socket stays unconnected: it still has to be
            # able to receive from the broadcast exchange that created it.
            self.sock.connect((host, port))
        self.out_seq = 0
        self.cmd_seq = 0
        self.buffer = b""
        self.seen: list[str] = []

    def close(self) -> None:
        self.sock.close()

    def _send(self, data: bytes) -> None:
        if self.adopted:
            self.sock.sendto(data, (self.host, self.port))
        else:
            self.sock.send(data)

    def _recv(self, size: int = 4096) -> bytes:
        """Return the next packet from our peer, ignoring anything else."""
        if not self.adopted:
            return self.sock.recv(size)
        while True:
            packet, (rhost, rport) = self.sock.recvfrom(size)
            if rhost == self.host:
                return packet

    def _block(self, subcmd: int, text: str) -> bytes:
        block = command_block(subcmd, text, self.cmd_seq)
        self.cmd_seq = (self.cmd_seq + 1) & 0xFFFF
        return block

    def _send_data(self, payload: bytes, repeat: int = 3) -> None:
        """Resend the identical datagram - never rebuild it.

        The device dedups on the sequence number, so a resend is a no-op. A
        rebuilt command with a fresh sequence would be executed twice, which
        for a door-release is not an academic distinction.
        """
        seq = self.out_seq
        self.out_seq = (self.out_seq + 1) & 0xFFFF
        frame = data_frame(CHANNEL_COMMAND, seq, payload)
        for _ in range(repeat):
            self._send(frame)

    def handshake(self) -> None:
        for _ in range(4):
            self._send(simple(MSG_CHECKCAM, pack_uid(self.uid)))
        self._send(simple(MSG_PING))
        time.sleep(0.3)

    def login(self, verbose: bool = False) -> tuple[bool, str]:
        blocks = self._block(SUBCMD_HELLO, "{}") + self._block(
            SUBCMD_LOGIN,
            '{"username":"%s","auth":"%s"}' % (self.username, self.auth),
        )
        self._send_data(blocks)

        end = time.time() + LOGIN_TIMEOUT
        next_ping = time.time() + PING_INTERVAL
        while time.time() < end:
            now = time.time()
            if now >= next_ping:
                self._send(simple(MSG_PING))
                next_ping = now + PING_INTERVAL
            self.sock.settimeout(min(0.3, max(0.05, end - now)))
            try:
                packet = self._recv()
            except TimeoutError:
                continue
            except ConnectionRefusedError:
                return False, (
                    f"ICMP port-unreachable from {self.host}:{self.port} - nothing "
                    "is listening there. The port answered a probe but is not "
                    "serving a session."
                )
            except OSError as err:
                return False, f"socket error: {err}"

            if len(packet) < 2 or packet[0] != MAGIC:
                continue
            if verbose:
                name = TYPES.get(packet[1], f"0x{packet[1]:02x}")
                self.seen.append(name)

            parsed = parse_data(packet)
            if parsed is None:
                continue
            channel, seq, payload = parsed
            # Every d0 must be acked or the device stalls its send window.
            self._send(ack_frame(channel, [seq]))
            if channel != CHANNEL_COMMAND:
                continue

            self.buffer += payload
            blocks_in, self.buffer = parse_blocks(self.buffer)
            for subcmd, text in blocks_in:
                if verbose:
                    print(f"      <- subcmd 0x{subcmd:04x}  {text}")
                if subcmd != SUBCMD_LOGIN:
                    continue
                try:
                    ok = json.loads(text).get("auth") == "ok"
                except ValueError:
                    return False, f"unparseable login reply: {text!r}"
                return ok, text
        return False, f"no login response within {LOGIN_TIMEOUT:.0f}s"

    def teardown(self) -> None:
        """Skipping this leaves the video channel held and the next connect
        is refused with 'video busy'."""
        blocks = (
            self._block(SUBCMD_TALK_ACTION, '{"action":"stop"}')
            + self._block(SUBCMD_TALK_CHANNEL_OFF, '{"channel":"1"}')
            + self._block(SUBCMD_AUDIO_STOP, '{"channel":"0"}')
            + self._block(SUBCMD_VIDEO_STOP, '{"channel":"0"}')
        )
        try:
            self._send_data(blocks, repeat=4)
            time.sleep(0.2)
        except OSError:
            pass


def lan_search_keeping_socket(
    wait: float = 2.5,
) -> tuple[socket.socket, str, int] | None:
    """LAN search that hands back the socket the punch arrived on.

    The ordinary lan_search() closes it, which - if the device binds the
    session to the peer tuple it punched at - throws away the only socket that
    can use the offer.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))
    try:
        sock.sendto(simple(MSG_LAN_SEARCH), ("255.255.255.255", 32108))
        end = time.time() + wait
        while time.time() < end:
            sock.settimeout(max(0.05, end - time.time()))
            try:
                packet, (rhost, rport) = sock.recvfrom(2048)
            except (TimeoutError, OSError):
                break
            if len(packet) >= 2 and packet[0] == MAGIC:
                return sock, rhost, rport
    except OSError:
        pass
    sock.close()
    return None


def experiment_punch(args) -> None:
    """Is the LAN-search reply a usable session offer?

    The device answers LAN search with 0x41 - a punch packet, the same message
    a client sends to open a session - not the 0x31 announcement stock PPPP
    documents. So it may be offering a session rather than announcing itself,
    and every tool so far has discarded the socket that offer was made to.

    Three arms separate the two explanations:

      same socket, at once   works only if the offer is real and usable
      fresh socket, at once  works too => the socket does not matter, and the
                             earlier failures were about timing
      same socket, delayed   fails => the offer has a short lifetime
    """
    print("=" * 70)
    print("EXPERIMENT 1 - is the punch socket a usable session?")
    print("=" * 70)
    results: list[tuple[str, bool, str]] = []

    def arm(label: str, delay: float, reuse: bool) -> None:
        print(f"\n   {label}")
        found = lan_search_keeping_socket()
        if found is None:
            print("      no LAN search reply - cannot run this arm")
            results.append((label, False, "no reply"))
            return
        sock, host, port = found
        print(f"      punch from {host}:{port}")
        if delay:
            print(f"      waiting {delay:.0f}s before logging in...")
            time.sleep(delay)
        if reuse:
            session = Session(host, port, args.uid, args.auth, args.username, sock=sock)
        else:
            # Leave the punched socket open but unused, so the only difference
            # from the arm above is which socket does the talking.
            session = Session(host, port, args.uid, args.auth, args.username)
        try:
            session.handshake()
            ok, detail = session.login(verbose=args.verbose)
            print(f"      {'LOGIN OK' if ok else 'no login'}   {detail}")
            results.append((label, ok, detail))
            session.teardown()
        finally:
            session.close()
            if not reuse:
                sock.close()

    arm("A. same socket, immediately", 0.0, True)
    time.sleep(1)
    arm("B. fresh socket, immediately", 0.0, False)
    time.sleep(1)
    arm("C. same socket, after 5s", 5.0, True)

    print()
    print("   " + "-" * 64)
    same_now = next((ok for label, ok, _ in results if label.startswith("A")), False)
    fresh_now = next((ok for label, ok, _ in results if label.startswith("B")), False)
    same_late = next((ok for label, ok, _ in results if label.startswith("C")), False)
    if same_now and not fresh_now:
        print("   The session is bound to the socket that was punched. LAN search")
        print("   is a complete cloud-free session path - keep the socket and log")
        print("   in on it. Discovery and connection stop being separate steps.")
    elif same_now and fresh_now and not same_late:
        print("   The socket does not matter, but the offer expires. Log in at once")
        print("   and the cloud and the sweep are both unnecessary.")
    elif same_now and fresh_now and same_late:
        print("   It just works. The earlier failures were something else - most")
        print("   likely the punch port had already been reused or timed out.")
    elif not same_now and not fresh_now:
        print("   The punch socket serves no session however it is approached.")
        print("   LAN search really does give the IP and nothing more; the port")
        print("   has to come from the cloud or the sweep.")
    else:
        print("   Mixed result - see the arms above.")


def experiment_types(args, ports: list[int]) -> None:
    """Walk the whole f1 message-type space and see what answers.

    We have only ever sent four of 256 possible types. 32108 is bound and
    handles 0x30, so it plainly speaks something; this finds out what else,
    on both that port and the punch socket. Each type is sent alone and
    answered before the next, so replies attribute unambiguously.
    """
    print("=" * 70)
    print("EXPERIMENT 2 - which message types get an answer?")
    print("=" * 70)
    payloads = [("empty", b""), ("uid", pack_uid(args.uid))]

    print("   256 types x 2 payloads per port, one at a time so replies")
    print("   attribute unambiguously. About 30s per port.")
    for port in ports:
        print(f"\n   {args.host}:{port}")
        answered = 0
        for label, payload in payloads:
            for msg_type in range(256):
                if msg_type and msg_type % 64 == 0:
                    print(f"      ...{label} payload, type 0x{msg_type:02x}")
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind(("", 0))
                # A device on the LAN answers in about a millisecond; this is
                # generous already, and 512 probes make it add up.
                sock.settimeout(0.05)
                try:
                    sock.sendto(simple(msg_type, payload), (args.host, port))
                    while True:
                        try:
                            reply, (rhost, rport) = sock.recvfrom(2048)
                        except (TimeoutError, OSError):
                            break
                        if rhost != args.host or len(reply) < 2:
                            continue
                        answered += 1
                        name = TYPES.get(msg_type, f"0x{msg_type:02x}")
                        back = TYPES.get(reply[1], f"0x{reply[1]:02x}")
                        via = "" if rport == port else f" (from port {rport})"
                        print(
                            f"      sent {name:<12} +{label:<5} -> {back:<12}"
                            f"{via}  {reply[:28].hex(' ')}"
                        )
                        break
                finally:
                    sock.close()
        if not answered:
            print("      nothing answered any of the 512 probes")


def try_login(host: str, port: int, args) -> bool:
    print(f"\n   logging in to {host}:{port} ...")
    session = Session(host, port, args.uid, args.auth, args.username)
    try:
        session.handshake()
        ok, detail = session.login(verbose=args.verbose)
        if ok:
            print(f"   LOGIN OK          {detail}")
        else:
            print(f"   login failed      {detail}")
        if args.verbose and session.seen:
            print(f"      packets seen: {', '.join(session.seen[:20])}")
        session.teardown()
        return ok
    finally:
        session.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--auth",
        default=DEFAULT_AUTH,
        help="32-hex-char auth hash (defaults to the captured one)",
    )
    ap.add_argument("--uid", default=DEFAULT_UID)
    ap.add_argument("--host", help="device LAN IP")
    ap.add_argument("--port", type=int, help="skip discovery entirely")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--no-cloud", action="store_true")
    ap.add_argument("--no-sweep", action="store_true", help="skip the 30-60s scan")
    ap.add_argument(
        "--first",
        action="store_true",
        help="stop at the first login that works (default: try all, to compare)",
    )
    ap.add_argument(
        "--experiment",
        choices=["login", "punch", "types", "all"],
        default="login",
        help=(
            "login: discover and log in (default). "
            "punch: is the LAN-search reply a usable session? "
            "types: which f1 message types get an answer?"
        ),
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print(f"\nUrmet login test - UID {args.uid}")

    if args.experiment in ("punch", "all"):
        experiment_punch(args)
        print()
    if args.experiment in ("types", "all"):
        if not args.host:
            found = lan_search()
            if found:
                args.host = found[0][0]
        if not args.host:
            print("   --experiment types needs --host (LAN search found nothing)")
            return 1
        ports = [32108]
        punch = lan_search()
        ports += [port for host, port in punch if host == args.host]
        if args.port:
            ports.append(args.port)
        experiment_types(args, sorted(set(ports)))
        print()
    if args.experiment != "login":
        return 0
    candidates: list[tuple[str, int, str]] = []
    host = args.host

    if args.port:
        candidates.append((host or args.host, args.port, "given"))
        print(f"\n   using {host}:{args.port} as given, no discovery")
    else:
        print("\n   LAN search (broadcast f1 30 to 32108)...")
        for reply_host, reply_port in lan_search():
            if args.host and reply_host != args.host:
                continue
            print(f"   reply from {reply_host}:{reply_port}")
            host = host or reply_host
            answered = probe(reply_host, reply_port, args.uid)
            if answered is None:
                print(f"      checkCam to {reply_port}: no session ack")
            else:
                if answered != reply_port:
                    print(f"      checkCam acked from port {answered} instead")
                candidates.append((reply_host, answered, "lan-search"))
        if not candidates and not host:
            print("   no LAN search reply and no --host given.")

        if not args.no_cloud:
            print("\n   cloud lookup via caycctv.com...")
            for cloud_host, cloud_port in cloud_lookup(args.uid):
                print(f"   cloud says {cloud_host}:{cloud_port}")
                host = host or cloud_host
                if (cloud_host, cloud_port) not in [(c[0], c[1]) for c in candidates]:
                    candidates.append((cloud_host, cloud_port, "cloud"))

        if not args.no_sweep and host:
            print(f"\n   sweeping {host} for every port that answers checkCam...")
            for port in sweep(host, args.uid):
                known = [(c[0], c[1]) for c in candidates]
                mark = "" if (host, port) in known else " (new)"
                print(f"   port {port} answers{mark}")
                if (host, port) not in known:
                    candidates.append((host, port, "sweep"))

    if not candidates:
        print("\n   nothing to try. Pass --host, or --host and --port.")
        return 1

    print()
    print("=" * 70)
    print(f"TRYING {len(candidates)} CANDIDATE(S)")
    print("=" * 70)
    results: list[tuple[str, int, str, bool]] = []
    for cand_host, cand_port, source in candidates:
        ok = try_login(cand_host, cand_port, args)
        results.append((cand_host, cand_port, source, ok))
        if ok and args.first:
            break

    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    for cand_host, cand_port, source, ok in results:
        state = "LOGIN OK" if ok else "no login"
        print(f"   {cand_host}:{cand_port:<6} via {source:<11} {state}")
    winners = [r for r in results if r[3]]
    print()
    if not winners:
        print("   Nothing logged in. Either the hash is wrong, or every port that")
        print("   answers checkCam is a responder rather than a session endpoint.")
        return 1
    sources = {r[2] for r in winners}
    print(f"   Working port(s): {', '.join(str(r[1]) for r in winners)}")
    if "lan-search" in sources:
        print("   LAN search found a port that logs in - discovery can stay local.")
    else:
        print("   LAN search did NOT produce a working port; it only tells us the")
        print(
            f"   device's IP. The working port came from: {', '.join(sorted(sources))}."
        )
        print("   -> the integration must stop treating the LAN-search reply port")
        print("      as the session port.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
