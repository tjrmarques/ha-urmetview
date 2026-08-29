"""The MPEG-TS fan-out, which decides whether a late joiner sees a picture.

Two failure modes, both observed against the real device:

  mid-packet start   the stream is 188-byte packets and ffmpeg's reads land
                     anywhere, so a consumer handed a raw chunk boundary
                     starts mid-header - "Failed to parse header of NALU
                     (type 0)"
  mid-GOP start      P-frames referring to a keyframe the consumer never saw,
                     which shows one frame and then nothing

    uv run tests/test_media.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

COMPONENT = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "urmetview"
)

# media.py needs no Home Assistant, but importing it through the package would
# run __init__.py, which does. Stand in a bare package with the right __path__
# so the relative imports resolve and nothing else is executed.
_pkg = types.ModuleType("urmetview")
_pkg.__path__ = [str(COMPONENT)]
sys.modules.setdefault("urmetview", _pkg)

_spec = importlib.util.spec_from_file_location(
    "urmetview.media", COMPONENT / "media.py"
)
media = importlib.util.module_from_spec(_spec)
sys.modules["urmetview.media"] = media
assert _spec.loader is not None
_spec.loader.exec_module(media)

MAX_CLIENT_BACKLOG = media.MAX_CLIENT_BACKLOG
TS_PACKET_SIZE = media.TS_PACKET_SIZE
MediaPipeline = media.MediaPipeline


def ts_packet(pid: int, fill: int = 0) -> bytes:
    """One well-formed 188-byte transport packet with the given PID."""
    header = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10])
    return header + bytes([fill]) * (TS_PACKET_SIZE - 4)


class _FakeTransport:
    def __init__(self, backlog: int = 0) -> None:
        self._backlog = backlog
        self.aborted = False

    def get_write_buffer_size(self) -> int:
        return self._backlog

    def abort(self) -> None:
        self.aborted = True


class _FakeWriter:
    def __init__(self, backlog: int = 0) -> None:
        self.transport = _FakeTransport(backlog)
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data


def _pipeline() -> MediaPipeline:
    return MediaPipeline("/bin/false", enable_audio=False)


def test_pending_consumer_waits_for_a_pat() -> None:
    """No PAT in the batch means nothing is sent - not a mid-GOP start."""
    pipeline = _pipeline()
    client = _FakeWriter()
    pipeline._pending_clients.add(client)

    pipeline._release_pending(ts_packet(0x0100) * 3)
    assert client.written == b""
    assert client in pipeline._pending_clients


def test_consumer_is_admitted_exactly_at_the_pat() -> None:
    """The first byte a consumer sees must be the start of the PAT packet."""
    pipeline = _pipeline()
    client = _FakeWriter()
    pipeline._pending_clients.add(client)

    batch = ts_packet(0x0100, 0xAA) + ts_packet(0x0000, 0xBB) + ts_packet(0x0100, 0xCC)
    pipeline._release_pending(batch)

    assert client.written == batch[TS_PACKET_SIZE:], "did not start at the PAT"
    assert client.written[0] == 0x47
    assert len(client.written) % TS_PACKET_SIZE == 0
    assert client in pipeline._out_clients
    assert not pipeline._pending_clients


def test_admitted_consumer_is_not_sent_the_batch_twice() -> None:
    """_release_pending writes the tail itself; _broadcast must not repeat it."""
    pipeline = _pipeline()
    client = _FakeWriter()
    pipeline._pending_clients.add(client)
    batch = ts_packet(0x0000) + ts_packet(0x0100)

    pipeline._release_pending(batch)
    first = client.written
    # The pump calls _broadcast next, over the same batch.
    pipeline._broadcast(batch)
    assert client.written == first + batch


def test_a_backed_up_consumer_is_dropped_not_buffered() -> None:
    pipeline = _pipeline()
    client = _FakeWriter(backlog=MAX_CLIENT_BACKLOG + 1)
    pipeline._out_clients.add(client)

    pipeline._broadcast(ts_packet(0x0100))
    assert client.written == b"", "wrote to a consumer that is not keeping up"
    assert client.transport.aborted
    assert client not in pipeline._out_clients


def test_a_healthy_consumer_is_kept() -> None:
    pipeline = _pipeline()
    client = _FakeWriter(backlog=1024)
    pipeline._out_clients.add(client)

    packet = ts_packet(0x0100)
    pipeline._broadcast(packet)
    assert client.written == packet
    assert client in pipeline._out_clients


def test_a_closed_consumer_is_discarded_quietly() -> None:
    class _Broken(_FakeWriter):
        def write(self, data: bytes) -> None:
            raise ConnectionResetError("gone")

    pipeline = _pipeline()
    client = _Broken()
    pipeline._out_clients.add(client)
    pipeline._broadcast(ts_packet(0x0100))
    assert client not in pipeline._out_clients


def test_relay_url_is_stable_across_restarts() -> None:
    """go2rtc caches the stream by source URL, so the port must not move."""
    import asyncio

    async def run() -> None:
        pipeline = _pipeline()
        await pipeline.async_ensure_relay()
        first = pipeline.stream_url
        assert first.endswith(str(pipeline.out_port))

        await pipeline.async_stop()
        assert pipeline.stream_url == first, "the relay port moved on stop"

        await pipeline.async_ensure_relay()
        assert pipeline.stream_url == first, "the relay port moved on restart"

        await pipeline.async_shutdown()
        assert pipeline.out_port == 0

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
