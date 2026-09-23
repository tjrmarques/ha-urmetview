"""Turning the device's raw streams into something Home Assistant can play.

The device gives us two independent byte streams - H.264 Annex-B video and
G.711 mu-law audio - with no container and no timestamps we trust. Home
Assistant wants a single URL. So:

    device --> [video TCP] --\\
                              ffmpeg (mux, copy video, encode audio to AAC)
    device --> [audio TCP] --/          |
                                        v
                              MPEG-TS --> local TCP relay --> HA / go2rtc

``stream_source()`` returns the relay's address, which HA hands to go2rtc as an
``ffmpeg:`` source, giving WebRTC playback with audio.

Two things here are less obvious than they look:

* **ffmpeg probes its inputs in order and blocks on a silent one.** If the
  device sends no audio, ffmpeg never finishes opening input 2 and no video
  ever comes out either. A filler task keeps mu-law silence flowing whenever
  real audio is not arriving, so that cannot happen.
* **Video is not forwarded until the first keyframe.** Starting mid-GOP feeds
  ffmpeg P-frames that reference a picture it never saw.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable

from .urmet.const import AUDIO_FRAME_BYTES, AUDIO_SAMPLE_RATE

_LOGGER = logging.getLogger(__name__)

#: MPEG-TS is a fixed 188-byte packet stream; consumers must be handed whole
#: packets or they start mid-header and have to resynchronise.
TS_PACKET_SIZE = 188
#: The Program Association Table. ffmpeg emits it immediately before a
#: keyframe, so it is the safe place to admit a new consumer.
TS_PID_PAT = 0x0000
#: Give up on a consumer whose socket backlog passes this, rather than buffer
#: without limit for a reader that has stopped reading.
MAX_CLIENT_BACKLOG = 4 * 1024 * 1024

_RATIONAL_RE = re.compile(r"[1-9]\d*/[1-9]\d*")


#: mu-law silence. Writing zero bytes would decode as a loud constant tone.
SILENCE_BYTE = 0xFF
SILENCE_FRAME = bytes([SILENCE_BYTE]) * AUDIO_FRAME_BYTES
AUDIO_FRAME_INTERVAL = AUDIO_FRAME_BYTES / AUDIO_SAMPLE_RATE

#: How long after real audio we keep quiet before filling with silence. Slightly
#: over one frame, so normal jitter does not trigger the filler.
SILENCE_GRACE = AUDIO_FRAME_INTERVAL * 2

FFMPEG_START_TIMEOUT = 15.0


class MediaPipeline:
    """Muxes the device's streams and serves them on a local TCP port."""

    def __init__(
        self,
        ffmpeg_binary: str,
        enable_audio: bool = True,
        pixel_aspect: str = "",
    ) -> None:
        self._ffmpeg_binary = ffmpeg_binary
        self._enable_audio = enable_audio
        self._pixel_aspect = pixel_aspect

        self._video_server: asyncio.AbstractServer | None = None
        self._audio_server: asyncio.AbstractServer | None = None
        self._out_server: asyncio.AbstractServer | None = None
        self._process: asyncio.subprocess.Process | None = None

        self._video_writer: asyncio.StreamWriter | None = None
        self._audio_writer: asyncio.StreamWriter | None = None
        self._out_clients: set[asyncio.StreamWriter] = set()
        self._pending_clients: set[asyncio.StreamWriter] = set()

        self._pump_task: asyncio.Task[None] | None = None
        self._silence_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        self._seen_keyframe = False
        self._last_audio = 0.0
        self._running = False

        self.video_port = 0
        self.audio_port = 0
        self.out_port = 0

        self.frames = 0
        self.keyframes = 0

        #: Called when the pipeline decides it needs the device stream running
        #: again (e.g. ffmpeg was restarted).
        self.on_restart: Callable[[], None] | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def client_count(self) -> int:
        """Consumers currently attached to the muxed output.

        This is the honest measure of "is anyone watching": go2rtc, a snapshot
        grab and the stream component all show up here as real TCP
        connections, so the device stream can be released on it without
        reference counting that can leak.
        """
        return len(self._out_clients)

    @property
    def ffmpeg_binary(self) -> str:
        return self._ffmpeg_binary

    @property
    def stream_url(self) -> str:
        return f"tcp://127.0.0.1:{self.out_port}"

    async def async_ensure_relay(self) -> None:
        """Open the output relay, once, and keep it for the entry's lifetime.

        The port has to stay put. go2rtc caches a stream by its source URL, so
        a relay that moves on every restart leaves it holding a producer that
        points at a closed port - which shows up as a stream whose producer
        list no longer parses, and playback failing while snapshots still
        work, because snapshots do not go through go2rtc.
        """
        if self._out_server is not None:
            return
        self._out_server = await asyncio.start_server(
            self._on_out_connect, "127.0.0.1", 0
        )
        self.out_port = self._out_server.sockets[0].getsockname()[1]
        _LOGGER.debug("Stream relay listening on %s", self.out_port)

    # -- lifecycle ----------------------------------------------------------

    async def async_start(self) -> None:
        """Bring up the sockets and ffmpeg. Safe to call when already running."""
        if self._running:
            return
        loop = asyncio.get_running_loop()

        self._video_server = await asyncio.start_server(
            self._on_video_connect, "127.0.0.1", 0
        )
        self.video_port = self._video_server.sockets[0].getsockname()[1]

        if self._enable_audio:
            self._audio_server = await asyncio.start_server(
                self._on_audio_connect, "127.0.0.1", 0
            )
            self.audio_port = self._audio_server.sockets[0].getsockname()[1]

        await self.async_ensure_relay()

        self._seen_keyframe = False
        self._last_audio = loop.time()
        self._running = True

        await self._async_spawn_ffmpeg()
        if self._enable_audio:
            self._silence_task = loop.create_task(self._async_silence_filler())

        _LOGGER.debug(
            "Media pipeline up: video=%s audio=%s out=%s",
            self.video_port,
            self.audio_port,
            self.out_port,
        )

    def _aspect_args(self) -> list[str]:
        """Bitstream-filter arguments for the aspect correction, if any.

        The device sends 960x240 with no aspect information, so players assume
        square pixels and stretch it. h264_metadata rewrites the SPS field in
        the bitstream, which keeps -c:v copy - a scale filter would force a
        re-encode for a one-field correction.

        The value must be a rational written with a slash. A colon is
        ffmpeg's own option separator inside a filter spec, so "1:2" parses as
        sample_aspect_ratio=1 followed by a nameless option "2", and ffmpeg
        then refuses to open the output at all - which takes video and
        snapshots with it. "1:2" is the natural way to write it, so accept it
        and convert; anything that is still not a rational is dropped with a
        warning rather than passed through to break the pipeline.
        """
        value = (self._pixel_aspect or "").strip().replace(":", "/")
        if not value:
            return []
        if not _RATIONAL_RE.fullmatch(value):
            _LOGGER.warning(
                "Ignoring picture aspect correction %r: expected a ratio like "
                "1:2 or 4/3",
                self._pixel_aspect,
            )
            return []
        return ["-bsf:v", f"h264_metadata=sample_aspect_ratio={value}"]

    async def _async_spawn_ffmpeg(self) -> None:
        args = [
            self._ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
        ]
        if self._enable_audio:
            args += [
                # Deliberately NO -use_wallclock_as_timestamps here. ffmpeg
                # samples wallclock once per underlying socket read(), not
                # once per logical 320-byte packet - a TCP read can scoop up
                # several already-queued packets in one call, and they all
                # land on the exact same pts. Confirmed directly with -af
                # ashowinfo spliced ahead of the encoder: 13 consecutive
                # packets, each with different real content, all landing on
                # one pts. That forces ffmpeg's "Queue input is backward in
                # time" correction (silently bumping DTS by +1 tick,
                # repeatedly) on every later packet in the burst, which is
                # what produced a multi-second, growing audio lag - not the
                # AAC resample step, not input order, not device-side delay.
                # Audio's format is fully declared (-ar -ac), so ffmpeg
                # derives pts purely from sample count instead - immune to
                # read-batching because it never looks at wallclock at all.
                # Measured over a 42s capture: video-vs-audio offset stays
                # bounded at 0.02-0.45s with no growth trend, even with 171
                # real device delivery gaps over 100ms in that same run.
                "-f",
                "mulaw",
                "-ar",
                str(AUDIO_SAMPLE_RATE),
                "-ac",
                "1",
                "-thread_queue_size",
                "512",
                "-i",
                f"tcp://127.0.0.1:{self.audio_port}",
            ]
        args += [
            # The device's H.264 has no container timing, so let ffmpeg stamp
            # arrival time rather than trusting absent timestamps.
            "-use_wallclock_as_timestamps",
            "1",
            # Inputs are opened in order, and ffmpeg finishes probing one
            # before connecting to the next - video used to be listed first,
            # so it ate the whole probe budget and audio waited behind it,
            # measured at 8.2s on a live device during which nothing was
            # muxed at all. Audio goes first now (above): its format is
            # fully declared, so it opens near-instantly and no longer
            # blocks behind video. Video is free to get a generous probe
            # without holding audio's connection hostage.
            "-analyzeduration",
            "0",
            # Not smaller: at 32 bytes ffmpeg cannot estimate the frame rate
            # and says so. 100KB still completes almost immediately on a live
            # stream and keeps the estimate.
            "-probesize",
            "100000",
            # A live input that blocks stalls the whole mux, video included.
            "-thread_queue_size",
            "512",
            "-f",
            "h264",
            "-i",
            f"tcp://127.0.0.1:{self.video_port}",
        ]
        args += ["-c:v", "copy"]
        args += self._aspect_args()
        if self._enable_audio:
            args += ["-c:a", "aac", "-b:a", "64k", "-ar", "16000"]
            # The muxer interleaves by DTS, so while audio is behind, video is
            # buffered rather than written - the picture simply stops. It is
            # bounded by max_interleave_delta, but the default bound is ten
            # seconds. Measured on this exact pipeline: audio going quiet at
            # t=2s stopped all output from 2s to 9s, then it resumed. At 100ms
            # the same test produces no gap at all.
            #
            # Losing strict interleaving costs nothing here: this is a live
            # stream going straight to a player, not a file to seek around in.
            args += ["-max_interleave_delta", "100000"]
        args += [
            "-f",
            "mpegts",
            # Repeat the headers so a client joining late can start decoding
            # without waiting for the next natural PAT/PMT.
            "-mpegts_flags",
            "+resend_headers",
            "-pat_period",
            "0.5",
            "pipe:1",
        ]

        _LOGGER.debug("Starting ffmpeg: %s", " ".join(args))
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        loop = asyncio.get_running_loop()
        self._pump_task = loop.create_task(self._async_pump_output())
        self._stderr_task = loop.create_task(self._async_drain_stderr())

    async def async_stop(self) -> None:
        """Tear everything down. Safe to call repeatedly."""
        self._running = False

        for task in (self._silence_task, self._pump_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._silence_task = self._pump_task = self._stderr_task = None

        if self._process is not None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            self._process = None

        for writer in (self._video_writer, self._audio_writer):
            if writer is not None:
                await _close_writer(writer)
        self._video_writer = self._audio_writer = None

        for client in list(self._out_clients | self._pending_clients):
            await _close_writer(client)
        self._out_clients.clear()
        self._pending_clients.clear()

        # The relay deliberately stays open - see async_ensure_relay.
        for server in (self._video_server, self._audio_server):
            if server is not None:
                server.close()
                with contextlib.suppress(Exception):
                    await server.wait_closed()
        self._video_server = self._audio_server = None

        _LOGGER.debug("Media pipeline stopped")

    async def async_shutdown(self) -> None:
        """Stop everything, relay included. For unload, not for going idle."""
        await self.async_stop()
        if self._out_server is not None:
            self._out_server.close()
            with contextlib.suppress(Exception):
                await self._out_server.wait_closed()
            self._out_server = None
            self.out_port = 0

    # -- ffmpeg input sockets ----------------------------------------------

    async def _on_video_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        _LOGGER.debug("ffmpeg connected to the video input")
        self._video_writer = writer
        with contextlib.suppress(Exception):
            await reader.read()

    async def _on_audio_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        _LOGGER.debug("ffmpeg connected to the audio input")
        self._audio_writer = writer
        with contextlib.suppress(Exception):
            await reader.read()

    # -- feeding from the device -------------------------------------------

    def feed_video(self, data: bytes, frame_start: bool, keyframe: bool) -> None:
        """Called from the session's receive path. Must not block."""
        if frame_start:
            self.frames += 1
            if keyframe:
                self.keyframes += 1
                self._seen_keyframe = True

        # Everything before the first keyframe references a picture ffmpeg has
        # never seen, so it is noise at best.
        if not self._seen_keyframe:
            return

        writer = self._video_writer
        if writer is None:
            return
        try:
            writer.write(data)
        except (OSError, ConnectionError):
            _LOGGER.debug("Video input closed by ffmpeg")
            self._video_writer = None

    def feed_audio(self, data: bytes) -> None:
        """Called from the session's receive path. Must not block."""
        if not self._enable_audio:
            return
        writer = self._audio_writer
        if writer is None:
            return
        self._last_audio = asyncio.get_running_loop().time()
        try:
            writer.write(data)
        except (OSError, ConnectionError):
            _LOGGER.debug("Audio input closed by ffmpeg")
            self._audio_writer = None

    async def _async_silence_filler(self) -> None:
        """Keep mu-law flowing when the device is quiet.

        Without this, a device that sends no audio leaves ffmpeg blocked
        probing its second input, and the video never reaches Home Assistant
        either - a total media failure caused entirely by silence.
        """
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                await asyncio.sleep(AUDIO_FRAME_INTERVAL)
                writer = self._audio_writer
                if writer is None:
                    continue
                if loop.time() - self._last_audio < SILENCE_GRACE:
                    continue
                try:
                    writer.write(SILENCE_FRAME)
                    self._last_audio = loop.time()
                except (OSError, ConnectionError):
                    self._audio_writer = None
        except asyncio.CancelledError:
            raise

    # -- output relay -------------------------------------------------------

    async def _on_out_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.debug("Stream consumer connected: %s", peer)
        # Held back until the next PAT - see _async_pump_output.
        self._pending_clients.add(writer)
        try:
            await reader.read()
        except (OSError, ConnectionError):
            pass
        finally:
            self._out_clients.discard(writer)
            self._pending_clients.discard(writer)
            await _close_writer(writer)
            _LOGGER.debug("Stream consumer disconnected: %s", peer)

    async def _async_pump_output(self) -> None:
        """Fan ffmpeg's MPEG-TS out, starting each consumer at a PAT.

        Two things have to be true for a late joiner to decode anything, and
        neither comes for free from copying chunks as they arrive.

        The stream is 188-byte packets, and ffmpeg's reads land wherever they
        land, so a consumer handed a chunk boundary starts mid-packet and has
        to resynchronise - which is where "Failed to parse header of NALU
        (type 0)" comes from. So output is reassembled into whole packets
        before it goes anywhere.

        And a consumer that starts mid-GOP has P-frames referring to a
        keyframe it never saw, which is a first frame and then nothing. New
        consumers therefore wait, in _pending_clients, until the next PAT -
        at most half a second, given -pat_period 0.5 - since ffmpeg emits the
        table pair immediately before a keyframe.
        """
        process = self._process
        if process is None or process.stdout is None:
            return
        buffer = b""
        try:
            while True:
                chunk = await process.stdout.read(16384)
                if not chunk:
                    break
                buffer += chunk

                # Resynchronise if ffmpeg's first bytes are not a packet start.
                if buffer[:1] != b"\x47":
                    sync = buffer.find(b"\x47")
                    if sync == -1:
                        buffer = b""
                        continue
                    buffer = buffer[sync:]

                whole = len(buffer) - (len(buffer) % TS_PACKET_SIZE)
                if not whole:
                    continue
                packets, buffer = buffer[:whole], buffer[whole:]

                just_joined: frozenset[asyncio.StreamWriter] = frozenset()
                if self._pending_clients:
                    just_joined = self._release_pending(packets)
                self._broadcast(packets, skip=just_joined)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Media output pump failed")

    def _release_pending(self, packets: bytes) -> frozenset[asyncio.StreamWriter]:
        """Admit waiting consumers from the first PAT in this batch.

        Returns who was just admitted, so the caller's _broadcast for this
        same batch can skip them - they were already sent everything from
        the PAT onward here, which is exactly what a joining consumer needs
        (not the whole batch, which can start before the PAT with packets
        left over from the previous cycle). Sending the unsliced batch to
        them too, right after, was a real bug: confirmed live against a
        real go2rtc consumer as "Packet corrupt", "non-existing PPS 0
        referenced", "no frame!", then a dropped connection - a garbled,
        doubled-up first GOP for every new joiner. Silent until this
        session, because this whole path was previously exercised only in
        synthetic single-batch tests, never against a real streaming
        consumer end to end.
        """
        for offset in range(0, len(packets), TS_PACKET_SIZE):
            packet = packets[offset : offset + TS_PACKET_SIZE]
            # PID is the low 13 bits of bytes 1-2; PID 0 is the PAT.
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            if pid != TS_PID_PAT:
                continue
            joining, self._pending_clients = self._pending_clients, set()
            for client in joining:
                self._out_clients.add(client)
                self._write(client, packets[offset:])
            _LOGGER.debug("Admitted %s consumer(s) at a PAT", len(joining))
            return frozenset(joining)
        return frozenset()

    def _broadcast(
        self, packets: bytes, skip: frozenset[asyncio.StreamWriter] = frozenset()
    ) -> None:
        for client in list(self._out_clients):
            if client in skip:
                continue
            self._write(client, packets)

    def _write(self, client: asyncio.StreamWriter, data: bytes) -> None:
        """Write, dropping a consumer that cannot keep up.

        Without the backlog check a stalled reader is buffered without limit,
        which trades a stuttering picture for unbounded memory.
        """
        try:
            transport = client.transport
            if (
                transport is not None
                and transport.get_write_buffer_size() > MAX_CLIENT_BACKLOG
            ):
                _LOGGER.debug("Dropping a stream consumer that fell too far behind")
                self._out_clients.discard(client)
                self._pending_clients.discard(client)
                transport.abort()
                return
            client.write(data)
        except (OSError, ConnectionError):
            self._out_clients.discard(client)
            self._pending_clients.discard(client)

    async def _async_drain_stderr(self) -> None:
        """Surface ffmpeg's complaints instead of letting the pipe fill up.

        A full stderr pipe deadlocks ffmpeg, so this must run even if nobody
        reads the logs.
        """
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text:
                    _LOGGER.debug("ffmpeg: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.debug("ffmpeg stderr reader stopped", exc_info=True)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()
