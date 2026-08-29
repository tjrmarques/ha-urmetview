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

    # find the device, then log in - the normal case
    uv run urmet_login.py --auth <32-hex-hash>

    # pin the port, skipping discovery
    uv run urmet_login.py --auth <hash> --host 10.0.50.6 --port 27754

    # try every port that answers, and report which one logs in
    uv run urmet_login.py --auth <hash> --host 10.0.50.6 --all

The hash is the 32 hex characters captured from a real app login.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import time

MAGIC = 0xF1
MSG_CHECKCAM = 0x41
MSG_SESSION_ACK = 0x42
MSG_DATA = 0xD0
MSG_ACK = 0xD1
MSG_PING = 0xE0
MSG_PING_ACK = 0xE1
MSG_LAN_SEARCH = 0x30

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


# --- the actual login -------------------------------------------------------


class Session:
    """Just enough session to log in, ack, ping and tear down cleanly."""

    def __init__(self, host: str, port: int, uid: str, auth: str, username: str):
        self.host, self.port = host, port
        self.uid, self.auth, self.username = uid, auth, username
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connected, so ICMP port-unreachable reaches us as an exception
        # instead of silently vanishing - that is the whole failure mode here.
        self.sock.connect((host, port))
        self.out_seq = 0
        self.cmd_seq = 0
        self.buffer = b""
        self.seen: list[str] = []

    def close(self) -> None:
        self.sock.close()

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
            self.sock.send(frame)

    def handshake(self) -> None:
        for _ in range(4):
            self.sock.send(simple(MSG_CHECKCAM, pack_uid(self.uid)))
        self.sock.send(simple(MSG_PING))
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
                self.sock.send(simple(MSG_PING))
                next_ping = now + PING_INTERVAL
            self.sock.settimeout(min(0.3, max(0.05, end - now)))
            try:
                packet = self.sock.recv(4096)
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
            self.sock.send(ack_frame(channel, [seq]))
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
    ap.add_argument("--auth", required=True, help="32-hex-char auth hash")
    ap.add_argument("--uid", default="URMABB-700171-SMCYN")
    ap.add_argument("--host", help="skip LAN search")
    ap.add_argument("--port", type=int, help="skip the probe too")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--all", action="store_true", help="try every port that answers")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print(f"\nUrmet login test - UID {args.uid}")

    if args.host and args.port:
        candidates = [(args.host, args.port)]
        print(f"\n   using {args.host}:{args.port} as given, no discovery")
    else:
        print("\n   LAN search (broadcast f1 30 to 32108)...")
        replies = lan_search()
        if args.host:
            replies = [r for r in replies if r[0] == args.host]
        if not replies:
            print("   no reply. Pass --host and --port, or check the subnet.")
            return 1
        candidates = []
        for host, port in replies:
            print(f"   reply from {host}:{port}")
            answered = probe(host, port, args.uid)
            if answered is None:
                print(f"      checkCam to {port}: no session ack")
                print("      -> the LAN-search port is not a session port. That is")
                print("         the thing to know: discovery must treat the reply as")
                print("         'the device is at this IP', not 'the session is here'.")
                candidates.append((host, port))
            elif answered == port:
                print(f"      checkCam to {port}: acked from the same port")
                candidates.append((host, port))
            else:
                print(f"      checkCam to {port}: acked from port {answered} instead")
                print(f"      -> {answered} is where the session should go")
                candidates.append((host, answered))

    ok = False
    for host, port in candidates:
        ok = try_login(host, port, args)
        if ok and not args.all:
            break

    print()
    print("=" * 70)
    if ok:
        print(f"  Login works. Use Host {candidates[0][0]} in the config flow and")
        print("  leave the port blank so discovery re-finds it each time.")
    else:
        print("  No candidate logged in. If the probe acked but the login did not")
        print("  answer, the port is serving checkCam and nothing else - which is")
        print("  what the integration keeps tripping over.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
