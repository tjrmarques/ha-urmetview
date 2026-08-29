"""Discovery tests, driven by a fake device on loopback.

The bug these exist for: the device does not always answer a probe from the
port it was asked on, and trusting the asked-for port sends the session to a
dead address. That only surfaces later as ECONNREFUSED and a login that never
completes, so it is worth pinning down here with a device that behaves the
same way.

    uv run tests/test_discovery.py
"""

from __future__ import annotations

import asyncio
import pathlib
import socket
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "urmetview")
)

from urmet import discovery  # noqa: E402
from urmet import protocol as p  # noqa: E402

UID = "URMABB-700171-SMCYN"
HOST = "127.0.0.1"


class _FakeDevice(asyncio.DatagramProtocol):
    """Listens on one port and replies from another, like the real device."""

    def __init__(self, reply_transport: asyncio.DatagramTransport | None) -> None:
        self._reply_transport = reply_transport
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 2 or data[0] != p.MAGIC or data[1] != p.MSG_CHECKCAM:
            return
        out = self._reply_transport or self.transport
        assert out is not None
        out.sendto(p.build_simple(p.MSG_SESSION_ACK), addr)


async def _spawn(reply_from_other_port: bool) -> tuple[int, int, list]:
    """Return (listen_port, reply_port, transports-to-close)."""
    loop = asyncio.get_running_loop()
    replier = None
    transports = []
    if reply_from_other_port:
        reply_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=(HOST, 0)
        )
        transports.append(reply_transport)
        replier = reply_transport
    listen_transport, _ = await loop.create_datagram_endpoint(
        lambda: _FakeDevice(replier), local_addr=(HOST, 0)
    )
    transports.append(listen_transport)
    listen_port = listen_transport.get_extra_info("sockname")[1]
    reply_port = (
        replier.get_extra_info("sockname")[1] if replier is not None else listen_port
    )
    return listen_port, reply_port, transports


def test_probe_returns_the_port_that_answered() -> None:
    """A reply from elsewhere means "go here instead", not "yes, this port"."""

    async def run() -> None:
        listen_port, reply_port, transports = await _spawn(reply_from_other_port=True)
        try:
            assert listen_port != reply_port
            answered = await discovery.async_probe_port(HOST, listen_port, UID)
            assert answered == reply_port, f"expected {reply_port}, got {answered}"
        finally:
            for transport in transports:
                transport.close()

    asyncio.run(run())


def test_probe_returns_the_same_port_when_the_device_is_well_behaved() -> None:
    async def run() -> None:
        listen_port, _, transports = await _spawn(reply_from_other_port=False)
        try:
            assert await discovery.async_probe_port(HOST, listen_port, UID) == listen_port
        finally:
            for transport in transports:
                transport.close()

    asyncio.run(run())


def test_probe_returns_none_when_nothing_answers() -> None:
    """A closed port must not be reported as reachable."""

    async def run() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((HOST, 0))
        dead_port = sock.getsockname()[1]
        sock.close()
        assert await discovery.async_probe_port(HOST, dead_port, UID, timeout=0.4) is None

    asyncio.run(run())


def test_find_device_skips_excluded_addresses() -> None:
    """A retry must not pick the address that just refused the session."""

    async def run() -> None:
        listen_port, _, transports = await _spawn(reply_from_other_port=False)
        try:
            found = await discovery.async_find_device(
                UID, host=HOST, cached_port=listen_port, allow_cloud=False
            )
            assert found is not None and found.port == listen_port

            again = await discovery.async_find_device(
                UID,
                host=HOST,
                cached_port=listen_port,
                allow_cloud=False,
                exclude=[(HOST, listen_port)],
            )
            assert again is None, f"excluded address was returned anyway: {again}"
        finally:
            for transport in transports:
                transport.close()

    asyncio.run(run())


def test_find_device_excludes_by_the_answering_port() -> None:
    """Exclusion is on where the session would actually go, not what we asked."""

    async def run() -> None:
        listen_port, reply_port, transports = await _spawn(reply_from_other_port=True)
        try:
            again = await discovery.async_find_device(
                UID,
                host=HOST,
                cached_port=listen_port,
                allow_cloud=False,
                exclude=[(HOST, reply_port)],
            )
            assert again is None, f"excluded answering port was returned: {again}"
        finally:
            for transport in transports:
                transport.close()

    asyncio.run(run())


def _run_standalone() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {err}")
        else:
            print(f"ok   {name}")
    print("\n" + ("all passed" if not failures else f"{failures} failed"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
