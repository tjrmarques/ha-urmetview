"""Session behaviour that is easy to get wrong and invisible when it is.

    uv run tests/test_session.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from collections import deque

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "urmetview")
)

from urmet import UrmetError  # noqa: E402
from urmet.session import UrmetSession  # noqa: E402

UID = "URMABB-700171-SMCYN"
AUTH = "0" * 32


def _session() -> UrmetSession:
    return UrmetSession("127.0.0.1", 12345, UID, AUTH, "admin")


def test_connection_refused_fails_waiters_immediately() -> None:
    """ICMP port-unreachable means every retry will fail the same way.

    Without this the connect spends its whole retry budget - three eight-second
    timeouts - to learn what the first packet already proved.
    """

    async def run() -> None:
        session = _session()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        session._pending[0x000B] = deque([future])
        session.error_received(ConnectionRefusedError(111, "Connection refused"))
        assert future.done(), "the waiter was left hanging"
        try:
            future.result()
        except UrmetError as err:
            assert "session port" in str(err), str(err)
        else:
            raise AssertionError("expected the future to fail")

    asyncio.run(run())


def test_other_socket_errors_do_not_latch() -> None:
    """A transient error must not permanently mark the device unreachable."""

    async def run() -> None:
        session = _session()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        session._pending[0x000B] = deque([future])
        session.error_received(OSError("temporary glitch"))
        assert session._unreachable is None
        assert not future.done()
        future.cancel()

    asyncio.run(run())


def test_sending_aborts_once_the_port_is_known_dead() -> None:
    async def run() -> None:
        session = _session()
        session.error_received(ConnectionRefusedError(111, "Connection refused"))
        try:
            await session._async_send_blocks_wait([b"\x00"], 0x000B)
        except UrmetError as err:
            assert "session port" in str(err), str(err)
        else:
            raise AssertionError("expected the send to abort")

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
