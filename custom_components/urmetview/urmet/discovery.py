"""Finding the device, and finding the port it is currently listening on.

The session port is not fixed - the device picks a fresh one and it is not
announced on the LAN broadcast, so it has to be discovered every time. Four
strategies are implemented here, cheapest first:

1. :func:`async_lan_search` - PPPP's own LAN discovery, and **the one that
   works**. Broadcasting ``MSG_LAN_SEARCH`` to UDP 32108 makes the device
   answer from its current session port. Verified against the real device: it
   replied from 23117, which a full port scan independently confirmed as the
   session port. Entirely local, ~2s, and needs no cloud access - but only
   reaches the device if the client shares its subnet.
2. :func:`async_probe_port` - ask a candidate port and learn which port
   actually answers, which is not always the one asked.
3. :func:`async_cloud_lookup` - the app's rendezvous via Urmet's servers.
   Proven, but needs internet.
4. :func:`async_port_sweep` - brute force checkCam across a port range and see
   which one answers. Fully local, deterministic, 30-90s for the whole range.

:func:`async_listen_broadcast` is separate: it catches the device's periodic
announcement on UDP 6688, which gives the UID and LAN IP (but not the port).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Collection
from dataclasses import dataclass

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

CLOUD_HOSTS_DISPLAY = p.CLOUD_HOSTS

LAN_SEARCH_PORT = 32108
#: Ports worth trying for LAN search. 32108 is the PPPP standard; the others are
#: cheap to include and cost one datagram each.
LAN_SEARCH_CANDIDATE_PORTS = (32108, 32100, 32106, 32107)


@dataclass(frozen=True)
class Candidate:
    """A device address we might be able to open a session against."""

    host: str
    port: int
    source: str

    def __str__(self) -> str:
        return f"{self.host}:{self.port} (via {self.source})"


class _Collector(asyncio.DatagramProtocol):
    """Collects every datagram received, with its sender."""

    def __init__(self) -> None:
        self.packets: list[tuple[tuple[str, int], bytes]] = []

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets.append((addr, data))

    def error_received(self, exc: Exception) -> None:
        # ICMP port-unreachable is normal and expected during a sweep.
        _LOGGER.debug("Discovery socket error (usually harmless): %s", exc)


async def _open(
    *, broadcast: bool = False, local_port: int = 0
) -> tuple[asyncio.DatagramTransport, _Collector]:
    loop = asyncio.get_running_loop()
    transport, protocol_obj = await loop.create_datagram_endpoint(
        _Collector, local_addr=("0.0.0.0", local_port), allow_broadcast=broadcast
    )
    if broadcast:
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return transport, protocol_obj  # type: ignore[return-value]


# --- 1. PPPP LAN search -----------------------------------------------------


async def async_lan_search(
    timeout: float = 2.0,
    ports: tuple[int, ...] = LAN_SEARCH_CANDIDATE_PORTS,
    broadcast_addr: str = "255.255.255.255",
) -> list[Candidate]:
    """Broadcast ``MSG_LAN_SEARCH`` and see who answers.

    In PPPP the device replies from the port it is actually serving sessions on,
    which is exactly the value the cloud lookup exists to tell us. If it works
    here, the integration never needs Urmet's servers at all.

    **Verified against the real device.** It replies with a ``0x41`` carrying
    the short-form packed UID - not the ``0x31`` LAN_NOTIFY stock PPPP
    documents - so any ``f1`` reply is accepted rather than matching on type.
    A full port scan independently confirmed the reply's source port as the
    session port.

    Only reaches the device if the client shares its subnet; a broadcast cannot
    cross. That is why earlier captures showed nothing - they were all taken
    from a different subnet.

    Both probe encodings are still sent, since only one has been observed
    working and the other costs a single datagram.
    """
    transport, collector = await _open(broadcast=True)
    probes = (
        p.build_simple(0x30),  # f1 30 00 00, the common encoding
        bytes([0x30, 0x00]),  # bare, as some implementations send it
    )
    try:
        for probe in probes:
            for port in ports:
                with contextlib.suppress(OSError):
                    transport.sendto(probe, (broadcast_addr, port))
        await asyncio.sleep(timeout)
    finally:
        transport.close()

    found: dict[tuple[str, int], Candidate] = {}
    for (host, port), data in collector.packets:
        # Log anything at all - an unexpected reply shape is far more useful to
        # see than to filter away, given this path is unproven.
        _LOGGER.debug("LAN search: %s:%s sent %s", host, port, data[:32].hex(" "))
        if len(data) < 2 or data[0] != p.MAGIC:
            continue
        found.setdefault(
            (host, port), Candidate(host, port, f"lan-search type=0x{data[1]:02x}")
        )
    return list(found.values())


# --- 2. Verify a known port -------------------------------------------------


async def async_probe_port(
    host: str, port: int, uid: str, timeout: float = 1.5
) -> int | None:
    """Send checkCam and return the port the session ack came *from*.

    Returns the responding port rather than a yes/no, because the device does
    not always answer from the port it was asked on. Its LAN-search reply in
    particular can come from a short-lived socket, so trusting that port sends
    the session somewhere nothing is listening - which surfaces much later as
    ECONNREFUSED and a login that never completes.

    Answering from a different port is not a failure; it is the device saying
    where to go, so callers should use what comes back here.
    """
    transport, collector = await _open()
    payload = p.build_simple(p.MSG_CHECKCAM, p.pack_uid_short(uid))
    try:
        for _ in range(3):
            transport.sendto(payload, (host, port))
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for (src_host, src_port), data in collector.packets:
                if src_host != host or len(data) < 2 or data[0] != p.MAGIC:
                    continue
                if data[1] not in (p.MSG_SESSION_ACK, p.MSG_PING_ACK):
                    continue
                if src_port != port:
                    _LOGGER.debug(
                        "Probed %s:%s but the session ack came from port %s - "
                        "using that instead",
                        host,
                        port,
                        src_port,
                    )
                return src_port
            await asyncio.sleep(0.05)
    finally:
        transport.close()
    return None


# --- 3. Cloud rendezvous ----------------------------------------------------


async def async_cloud_lookup(uid: str, timeout: float = 4.0) -> list[Candidate]:
    """Ask Urmet's rendezvous servers where the device is.

    Prefers the RFC1918 candidate: the servers return both a LAN and a
    relay/public address, and only the LAN one works when we are local.
    """
    loop = asyncio.get_running_loop()
    servers: list[str] = []
    for hostname in p.CLOUD_HOSTS:
        try:
            infos = await loop.getaddrinfo(
                hostname, p.CLOUD_PORT, proto=socket.IPPROTO_UDP
            )
        except OSError as err:
            _LOGGER.debug("DNS lookup failed for %s: %s", hostname, err)
            continue
        servers.extend(info[4][0] for info in infos)
    if not servers:
        _LOGGER.debug(
            "Falling back to hardcoded cloud IPs; Urmet may have rotated them"
        )
        servers = list(p.CLOUD_FALLBACK_IPS)

    transport, collector = await _open()
    local_port = transport.get_extra_info("sockname")[1]
    try:
        hello = p.build_cloud_hello()
        for server in servers:
            with contextlib.suppress(OSError):
                transport.sendto(hello, (server, p.CLOUD_PORT))
        await asyncio.sleep(0.3)
        # The servers were tested against both UID packings from spec section
        # 2b - port at offset 20 and at offset 22 - and accept either. The
        # documented offset-20 form is used; pack_uid_long_alt is kept for
        # reference only.
        lookup = p.build_cloud_lookup(uid, local_port)
        for server in servers:
            with contextlib.suppress(OSError):
                transport.sendto(lookup, (server, p.CLOUD_PORT))
        await asyncio.sleep(timeout)
    finally:
        transport.close()

    candidates: list[Candidate] = []
    for _, data in collector.packets:
        response = p.parse_cloud_response(data)
        if response is None:
            continue
        if response.status is not None:
            if response.status == 0:
                _LOGGER.debug("Cloud accepted the lookup")
            else:
                _LOGGER.warning(
                    "Cloud rejected the lookup (status 0x%02x) - most likely the UID "
                    "packing",
                    response.status,
                )
            continue
        if response.is_candidate and response.host and response.port:
            candidate = Candidate(response.host, response.port, "cloud")
            if candidate not in candidates:
                candidates.append(candidate)

    candidates.sort(key=lambda c: not p.is_private_ip(c.host))
    return candidates


# --- 4. Local port sweep ----------------------------------------------------


async def async_port_sweep(
    host: str,
    uid: str,
    start: int = 1024,
    end: int = 65535,
    rate: int = 20000,
    settle: float = 2.0,
) -> list[Candidate]:
    """Send checkCam to every port in a range and see which answer.

    Deterministic and fully local. Returns **every** port that answered, in
    ascending order - the device keeps several sockets open (one per cloud
    rendezvous server, on the evidence) and all of them ack a checkCam, so a
    single reply is a candidate, not a conclusion.

    Pacing is against a deadline rather than a fixed sleep per burst. The
    obvious ``sleep(0.01)`` every N packets is what made this take 66s for a
    nominal 3000/s: the OS rounds each sleep up to its timer granularity, so
    the sleeps, not the packets, set the pace.
    """
    transport, collector = await _open()
    payload = p.build_simple(p.MSG_CHECKCAM, p.pack_uid_short(uid))
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        sent = 0
        for port in range(start, end + 1):
            try:
                transport.sendto(payload, (host, port))
            except OSError:
                # Buffer full; yield and retry this port rather than skip it.
                await asyncio.sleep(0.005)
                with contextlib.suppress(OSError):
                    transport.sendto(payload, (host, port))
            sent += 1
            if sent % 256 == 0:
                behind = started + sent / rate - loop.time()
                await asyncio.sleep(max(0.0, behind))
        _LOGGER.debug(
            "Swept %s ports on %s in %.1fs", sent, host, loop.time() - started
        )
        await asyncio.sleep(settle)
    finally:
        transport.close()

    found: dict[int, Candidate] = {}
    for (src_host, src_port), data in collector.packets:
        if src_host != host or len(data) < 2 or data[0] != p.MAGIC:
            continue
        if data[1] in (p.MSG_SESSION_ACK, p.MSG_PING_ACK):
            found.setdefault(src_port, Candidate(host, src_port, "port-sweep"))
    if len(found) > 1:
        _LOGGER.debug(
            "%s ports on %s answered: %s. Only one will accept a login, so all "
            "are tried in turn.",
            len(found),
            host,
            ", ".join(str(port) for port in sorted(found)),
        )
    return [found[port] for port in sorted(found)]


# --- LAN announcement -------------------------------------------------------


async def async_listen_broadcast(timeout: float = 35.0) -> p.DiscoveredDevice | None:
    """Wait for the device's periodic announcement on UDP 6688.

    Gives the UID, IP, MAC, firmware - and the device password in cleartext,
    which is worth knowing about but is not the login credential.

    The announcement is autonomous and fairly infrequent, so allow a generous
    timeout; it is not triggered by anything we send.
    """
    loop = asyncio.get_running_loop()
    transport, collector = await loop.create_datagram_endpoint(
        _Collector,
        local_addr=("0.0.0.0", p.DISCOVERY_PORT),
        allow_broadcast=True,
        reuse_port=hasattr(socket, "SO_REUSEPORT") or None,
    )
    try:
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for _, data in collector.packets:  # type: ignore[attr-defined]
                device = p.parse_discovery_broadcast(data)
                if device is not None:
                    return device
            await asyncio.sleep(0.2)
    finally:
        transport.close()
    return None


# --- Orchestration ----------------------------------------------------------


async def async_find_device(
    uid: str,
    host: str | None = None,
    cached_port: int | None = None,
    allow_cloud: bool = True,
    allow_sweep: bool = False,
    exclude: Collection[tuple[str, int]] = (),
) -> Candidate | None:
    """Try every strategy in order of cost and return the first that works.

    Logs the outcome of each step. Without that a failure is just "not found",
    which is indistinguishable between a routing problem, a blocked broadcast,
    a cloud outage and a device that is simply off.

    ``exclude`` skips addresses already known to be dead. An address can answer
    a probe and still refuse the session moments later, and without this a
    retry just rediscovers the same dead port and fails identically.
    """
    skip = set(exclude)

    def _accept(candidate: Candidate) -> Candidate | None:
        """Drop a candidate that is on the exclude list."""
        if (candidate.host, candidate.port) in skip:
            _LOGGER.debug("Skipping %s - already failed this round", candidate)
            return None
        return candidate

    if host and cached_port and (host, cached_port) not in skip:
        answered = await async_probe_port(host, cached_port, uid)
        if answered is not None and (
            found := _accept(Candidate(host, answered, "cached"))
        ):
            _LOGGER.debug("Found via supplied/cached address %s:%s", host, answered)
            return found
        _LOGGER.debug(
            "No reply from the supplied address %s:%s - the port may have changed",
            host,
            cached_port,
        )

    replies = await async_lan_search()
    if not replies:
        _LOGGER.debug(
            "LAN search got no reply. Expected if Home Assistant and the intercom "
            "are on different subnets or VLANs, since the broadcast cannot cross."
        )
    for candidate in replies:
        if (candidate.host, candidate.port) in skip:
            _LOGGER.debug("Skipping %s - already failed this round", candidate)
            continue
        answered = await async_probe_port(candidate.host, candidate.port, uid)
        if answered is not None and (
            found := _accept(Candidate(candidate.host, answered, candidate.source))
        ):
            _LOGGER.debug("Found via LAN search: %s", found)
            return found
        _LOGGER.debug("LAN search replied from %s but no session followed", candidate)

    if allow_cloud:
        candidates = await async_cloud_lookup(uid)
        if not candidates:
            _LOGGER.debug(
                "Cloud lookup returned no candidates. Check outbound UDP 32100 is "
                "allowed and that %s resolve.",
                ", ".join(CLOUD_HOSTS_DISPLAY),
            )
        for candidate in candidates:
            if (candidate.host, candidate.port) in skip:
                _LOGGER.debug("Skipping %s - already failed this round", candidate)
                continue
            answered = await async_probe_port(candidate.host, candidate.port, uid)
            if answered is not None and (
                found := _accept(Candidate(candidate.host, answered, "cloud"))
            ):
                _LOGGER.debug("Found via cloud lookup: %s", found)
                return found
            _LOGGER.debug(
                "Cloud offered %s but it did not answer - unreachable from here, "
                "or the address is a relay rather than the LAN one",
                candidate,
            )

    if allow_sweep and host:
        _LOGGER.debug("Sweeping ports on %s as a last resort", host)
        for candidate in await async_port_sweep(host, uid):
            if (found := _accept(candidate)) is not None:
                _LOGGER.debug("Found via port sweep: %s", found)
                return found

    _LOGGER.warning(
        "Could not locate the intercom by any method (LAN search, cloud lookup%s). "
        "Setting an explicit host and port in the integration options bypasses "
        "discovery entirely.",
        ", port sweep" if allow_sweep and host else "",
    )
    return None
