"""Turning the device's raw streams into something Home Assistant can play.

The device gives us two independent byte streams - H.264 Annex-B video and
G.711 mu-law audio - with no container and no timestamps we trust. Home
Assistant wants a single URL. So:

    device --> [video mux thread] --\\
                                     ffmpeg (mux, copy video, encode audio to AAC)
    device --> [audio mux thread] --/          |
                                                v
                                     MPEG-TS --> local TCP relay --> HA / go2rtc

``stream_source()`` returns the relay's address, which HA hands to go2rtc as an
``ffmpeg:`` source, giving WebRTC playback with audio.

Three things here are less obvious than they look:

* **Neither input trusts ffmpeg to invent a timestamp.** ffmpeg's own
  ``-use_wallclock_as_timestamps`` samples wallclock once per socket
  ``read()``, not once per logical frame - under bursty delivery (network
  jitter, our own reorder buffer releasing several already-queued frames at
  once), several distinct frames can land in one ``read()`` and get stamped
  with the same "now", forcing a "Non-monotonic DTS" correction that
  silently smears real content in presentation time. Confirmed for both
  streams against the real device. Instead, each stream is packetized
  ourselves (via PyAV, into a small container ffmpeg reads real timestamps
  from) with an explicit, real per-frame pts: video from our own
  receive-time (``time.monotonic()``, sampled once per real UDP datagram -
  immune to the read-batching problem because UDP never coalesces
  datagrams the way a TCP read can); audio from the device's own embedded
  clock (a genuine absolute Unix-seconds field plus an exact +40ms/frame
  sub-second field - confirmed reliable to the ms, unlike video's own
  embedded clock field, which turned out to update in unpredictable bursts
  rather than smoothly and was unusable for this).
* **PyAV's muxing is a blocking, synchronous library call**, so video/audio
  input handling runs in two dedicated background threads (not on the
  event loop) that each own a plain blocking socket ffmpeg connects to -
  ``feed_video``/``feed_audio`` (still called from the session's hot path,
  still must not block) only ever hand data to these threads through a
  thread-safe queue.
* **ffmpeg probes its inputs in order and blocks on a silent one.** If the
  device sends no audio, ffmpeg never finishes opening input 2 and no video
  ever comes out either. Audio is listed first on the command line because
  its format is always known immediately; video gets a generous probe
  without holding audio's connection hostage.
* **Video is not forwarded until the first keyframe.** Starting mid-GOP feeds
  ffmpeg P-frames that reference a picture it never saw.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import re
import socket
import threading
import time
from collections.abc import Callable
from fractions import Fraction

import av

from .urmet.const import AUDIO_SAMPLE_RATE
from .urmet.protocol import decode_audio_clock_ms

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

#: MPEG-TS' own native clock resolution. Used as the time_base for the video
#: packets muxed ourselves - matches what ffmpeg's own mpegts muxer would
#: use, minimising rounding at the container boundary.
VIDEO_TIME_BASE = Fraction(1, 90000)
#: mu-law's own native sample rate. One tick per sample, exact, no rounding.
AUDIO_TIME_BASE = Fraction(1, AUDIO_SAMPLE_RATE)

#: How long a mux thread waits on its queue before re-checking whether it
#: should stop. Bounds shutdown latency; short enough not to matter.
MUX_QUEUE_POLL = 0.5
#: How long async_stop waits for a mux thread to actually exit before giving
#: up on it (it is a daemon thread, so the process is never blocked by it
#: regardless - this only bounds how long a reload takes).
MUX_THREAD_JOIN_TIMEOUT = 3.0

FFMPEG_START_TIMEOUT = 15.0


class _SocketWriter:
    """Minimal file-like wrapper so PyAV can mux straight into ffmpeg's
    input socket. PyAV's av.open() only needs .write() from a streaming
    (non-seekable) output - no .seek()/.tell().
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)


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

        self._out_server: asyncio.AbstractServer | None = None
        self._process: asyncio.subprocess.Process | None = None

        self._out_clients: set[asyncio.StreamWriter] = set()
        self._pending_clients: set[asyncio.StreamWriter] = set()

        self._pump_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        self._seen_keyframe = False
        self._running = False

        self.video_port = 0
        self.audio_port = 0
        self.out_port = 0

        self.frames = 0
        self.keyframes = 0

        # -- video/audio input: dedicated threads, not the event loop -------
        # See the module docstring: PyAV's muxing is a blocking library
        # call, so it cannot run on the event loop. feed_video/feed_audio
        # (still called from the session's hot path) only ever enqueue.
        self._video_srv: socket.socket | None = None
        self._audio_srv: socket.socket | None = None
        self._video_conn: socket.socket | None = None
        self._audio_conn: socket.socket | None = None
        self._video_mux_thread: threading.Thread | None = None
        self._audio_mux_thread: threading.Thread | None = None

        # Complete, assembled frames land here (whole frames, not raw
        # chunks - the muxer needs one full access unit per packet).
        # Generous video cap (256): at most ~8 frames/s measured against
        # the real device, so this bounds backlog at ~30s worst case.
        # Audio cap (50) is ~2s at 40ms/frame.
        self._video_queue: queue.Queue = queue.Queue(maxsize=256)
        self._audio_queue: queue.Queue = queue.Queue(maxsize=50)

        # Accumulates the CURRENT in-progress frame's bytes across however
        # many chunks it arrives in (feed_video/feed_audio are called once
        # per chunk, not once per frame) - finalized into one queued item
        # the moment the next frame starts. Only ever touched from
        # feed_video/feed_audio, both called on the event loop thread, so
        # no lock is needed here.
        self._video_frame_buf = bytearray()
        self._video_frame_is_keyframe = False
        self._video_frame_recv_ms: float | None = None
        self._audio_frame_buf = bytearray()
        self._audio_frame_recv_ms: int | None = None

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

    @staticmethod
    def _listening_socket() -> tuple[socket.socket, int]:
        """A plain blocking listening socket on an ephemeral port.

        Not asyncio.start_server: the mux thread that accepts on this needs
        a real blocking socket to hand to PyAV (see _SocketWriter) and to
        block on with sendall() - both incompatible with the event loop.
        """
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        return srv, srv.getsockname()[1]

    async def async_start(self) -> None:
        """Bring up the sockets and ffmpeg. Safe to call when already running."""
        if self._running:
            return

        self._video_srv, self.video_port = self._listening_socket()
        if self._enable_audio:
            self._audio_srv, self.audio_port = self._listening_socket()

        await self.async_ensure_relay()

        self._seen_keyframe = False
        self._running = True

        # Fresh queues/buffers for this session - see async_stop, which
        # joins both mux threads before returning specifically so nothing
        # is still reading the old ones when this runs.
        self._video_queue = queue.Queue(maxsize=256)
        self._audio_queue = queue.Queue(maxsize=50)
        self._video_frame_buf = bytearray()
        self._video_frame_is_keyframe = False
        self._video_frame_recv_ms = None
        self._audio_frame_buf = bytearray()
        self._audio_frame_recv_ms = None

        self._video_mux_thread = threading.Thread(
            target=self._video_accept_and_mux, daemon=True
        )
        self._video_mux_thread.start()
        if self._enable_audio:
            self._audio_mux_thread = threading.Thread(
                target=self._audio_accept_and_mux, daemon=True
            )
            self._audio_mux_thread.start()

        await self._async_spawn_ffmpeg()

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
                "-thread_queue_size",
                "512",
                # Audio arrives pre-timestamped (see the module docstring
                # and _audio_accept_and_mux) with a real per-frame pts
                # decoded from the device's own embedded clock, wrapped in
                # a small NUT container by us - NUT, not mpegts: ffmpeg's
                # mpegts muxer has no defined stream_type for raw
                # pcm_mulaw ("Unsupported codec"), confirmed directly; NUT
                # is libavformat's own general-purpose container, built
                # for exactly this (arbitrary codecs with explicit
                # timestamps, streamable).
                "-f",
                "nut",
                "-i",
                f"tcp://127.0.0.1:{self.audio_port}",
            ]
        args += [
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
            # Video arrives pre-timestamped too (_video_accept_and_mux),
            # wrapped in a minimal MPEG-TS video stream carrying a real
            # per-frame pts - raw "-f h264" (an Annex-B elementary stream
            # with no timing field of its own) used to leave ffmpeg to
            # guess pts from -use_wallclock_as_timestamps, which samples
            # wallclock once per socket read() and collides multiple
            # distinct frames onto one identical DTS under bursty delivery
            # (the same mechanism that caused audio's own lag bug below,
            # confirmed for video against the real device with dozens of
            # "Non-monotonic DTS; previous: X, current: X" collisions per
            # session). Neither input needs -use_wallclock_as_timestamps
            # at all any more; both carry a real timestamp in the
            # container, and setting it here would make ffmpeg discard
            # that and guess again.
            "-f",
            "mpegts",
            "-i",
            f"tcp://127.0.0.1:{self.video_port}",
        ]
        args += ["-c:v", "copy"]
        args += self._aspect_args()
        if self._enable_audio:
            # aresample=async=1000: the AAC encoder wants fixed-size
            # (1024-sample) frames; our real per-packet pts (device-clock-
            # derived, ~320 samples but not perfectly uniform - real
            # jitter) confused its internal frame accumulation without
            # this, producing severe, repeated backward DTS jumps in the
            # final output - confirmed by reproducing it from a static
            # replay of an independently-verified-monotonic capture
            # (isolating it to the encode step, not live timing), and
            # confirming this exact flag eliminates it cleanly. async=1000
            # lets it gently stretch/compress (up to 1000 samples/s) to
            # reconcile the two instead of fighting over exact sample
            # boundaries.
            # volume=-4dB: ffmpeg's native AAC encoder overshoots past
            # 0dBFS on this content - confirmed via astats measurement on
            # a real capture, present identically with or without the
            # aresample fix above (so pre-existing, not caused by it): raw
            # mu-law decode peaks at a clean -0.17dB, but the same audio
            # round-tripped through AAC at these settings (64k/16kHz,
            # otherwise unchanged from before this session) comes back at
            # +2.7dB - actual clipping, not just close to the ceiling.
            # Reported live as "lots of noise... voice really low,
            # unintelligible", which matches clipped/distorted speech
            # better than a real gain problem: the raw signal already
            # sits right at full scale with no headroom, so the encoder's
            # own quantization has nowhere to absorb its reconstruction
            # error. -4dB gives it room (confirmed clean afterward, peak
            # back under 0dBFS with margin) at the cost of a few dB of
            # loudness - a smaller, more defensible trade than shipping
            # audio that measurably clips.
            args += ["-af", "aresample=async=1000,volume=-4dB"]
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

        for task in (self._pump_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._pump_task = self._stderr_task = None

        if self._process is not None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            self._process = None

        # Close everything the mux threads might be blocked on (accept(),
        # sendall()) so they notice _running is False promptly rather than
        # waiting out their own poll interval - then join them. This has to
        # finish before returning: async_start recreates the queues these
        # threads read from, and a thread still alive against the old queue
        # when that happens is a real race, not just a lingering log line.
        loop = asyncio.get_running_loop()
        for srv, conn, thread in (
            (self._video_srv, self._video_conn, self._video_mux_thread),
            (self._audio_srv, self._audio_conn, self._audio_mux_thread),
        ):
            for sock in (srv, conn):
                if sock is not None:
                    with contextlib.suppress(OSError):
                        sock.close()
            if thread is not None:
                await loop.run_in_executor(
                    None, thread.join, MUX_THREAD_JOIN_TIMEOUT
                )
        self._video_srv = self._audio_srv = None
        self._video_conn = self._audio_conn = None
        self._video_mux_thread = self._audio_mux_thread = None

        for client in list(self._out_clients | self._pending_clients):
            await _close_writer(client)
        self._out_clients.clear()
        self._pending_clients.clear()

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

    # -- video/audio input: accept + mux, each its own thread ---------------

    def _video_accept_and_mux(self) -> None:
        srv = self._video_srv
        if srv is None:
            return
        try:
            conn, _ = srv.accept()
        except OSError:
            return  # closed (shutdown) before ffmpeg ever connected
        conn.settimeout(8.0)
        self._video_conn = conn
        _LOGGER.debug("ffmpeg connected to the video input")
        try:
            self._video_mux_loop(conn)
        except Exception:
            _LOGGER.exception("Video mux thread failed")

    def _audio_accept_and_mux(self) -> None:
        srv = self._audio_srv
        if srv is None:
            return
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        conn.settimeout(8.0)
        self._audio_conn = conn
        _LOGGER.debug("ffmpeg connected to the audio input")
        try:
            self._audio_mux_loop(conn)
        except Exception:
            _LOGGER.exception("Audio mux thread failed")

    def _video_mux_loop(self, conn: socket.socket) -> None:
        """Own writer to ffmpeg's video input, running on its own thread.

        Frames are written as soon as they're dequeued - no pacing.
        Correctness comes from the pts value being right, not from write
        timing: ffmpeg reads a real, already-correct timestamp from the
        container, so it no longer matters how bursty delivery is.
        """
        container = None
        stream = None
        last_muxed_pts: int | None = None
        origin_ms: float | None = None

        while self._running:
            try:
                frame_bytes, is_keyframe, recv_ms = self._video_queue.get(
                    timeout=MUX_QUEUE_POLL
                )
            except queue.Empty:
                continue
            if origin_ms is None:
                origin_ms = recv_ms

            if container is None:
                try:
                    container = av.open(_SocketWriter(conn), mode="w", format="mpegts")
                    stream = container.add_stream("h264")
                    stream.time_base = VIDEO_TIME_BASE
                except Exception:
                    _LOGGER.exception("Failed to open video mux container")
                    container = None
                    continue

            pts = round((recv_ms - origin_ms) * VIDEO_TIME_BASE.denominator / 1000)
            if last_muxed_pts is not None and pts <= last_muxed_pts:
                # Should essentially never trigger (time.monotonic() is
                # strictly increasing at this resolution) - kept only as a
                # safety net, since the mpegts muxer requires strictly
                # increasing dts.
                pts = last_muxed_pts + 1
            last_muxed_pts = pts

            pkt = av.Packet(frame_bytes)
            pkt.stream = stream
            pkt.pts = pts
            pkt.dts = pts
            pkt.time_base = VIDEO_TIME_BASE
            pkt.is_keyframe = is_keyframe
            try:
                container.mux(pkt)
            except (OSError, ConnectionError, av.error.FFmpegError):
                _LOGGER.debug("Video input closed by ffmpeg")
                with contextlib.suppress(Exception):
                    container.close()
                container = None
                stream = None
                break

        if container is not None:
            with contextlib.suppress(Exception):
                container.close()

    def _audio_mux_loop(self, conn: socket.socket) -> None:
        """Own writer to ffmpeg's audio input. See _video_mux_loop."""
        container = None
        stream = None
        last_muxed_pts: int | None = None
        origin_ms: int | None = None

        while self._running:
            try:
                chunk, recv_ms = self._audio_queue.get(timeout=MUX_QUEUE_POLL)
            except queue.Empty:
                continue
            if recv_ms is None:
                # Only possible if the very first payload after connecting
                # was a continuation with no frame-start header yet - no
                # real pts to assign, so drop it rather than guess.
                continue
            if origin_ms is None:
                origin_ms = recv_ms

            if container is None:
                try:
                    container = av.open(_SocketWriter(conn), mode="w", format="nut")
                    stream = container.add_stream("pcm_mulaw", rate=AUDIO_SAMPLE_RATE)
                    stream.codec_context.layout = "mono"
                    stream.time_base = AUDIO_TIME_BASE
                except Exception:
                    _LOGGER.exception("Failed to open audio mux container")
                    container = None
                    continue

            pts = round((recv_ms - origin_ms) * AUDIO_TIME_BASE.denominator / 1000)
            if last_muxed_pts is not None and pts <= last_muxed_pts:
                pts = last_muxed_pts + 1
            last_muxed_pts = pts

            pkt = av.Packet(chunk)
            pkt.stream = stream
            pkt.pts = pts
            pkt.dts = pts
            pkt.time_base = AUDIO_TIME_BASE
            # Without this, the mu-law->AAC resample/encode chain has no
            # declared sample count per packet and has to guess - confirmed
            # directly as the cause of severe backward-DTS corruption in
            # the final AAC output otherwise. 1 byte of 8-bit mu-law is
            # exactly 1 sample.
            pkt.duration = len(chunk)
            try:
                container.mux(pkt)
            except (OSError, ConnectionError, av.error.FFmpegError):
                _LOGGER.debug("Audio input closed by ffmpeg")
                with contextlib.suppress(Exception):
                    container.close()
                container = None
                stream = None
                break

        if container is not None:
            with contextlib.suppress(Exception):
                container.close()

    # -- feeding from the device ---------------------------------------------

    def feed_video(self, data: bytes, frame_start: bool, keyframe: bool) -> None:
        """Called from the session's receive path. Must not block.

        A frame may span several chunks (a frame-start chunk, then zero or
        more continuation chunks). Only accumulates into
        _video_frame_buf; a new frame-start chunk finalizes whatever was
        accumulated for the PREVIOUS frame and queues it as one complete
        access unit for the video mux thread.

        The frame-start chunk's arrival is also where the real per-frame
        pts is captured (time.monotonic()) - the session calls this
        synchronously, once per real UDP datagram (which, unlike a TCP
        read(), never coalesces multiple datagrams into one call), so this
        is an accurate, un-batched per-frame sample even under bursty
        delivery. See the module docstring.
        """
        if frame_start:
            self._finalize_video_frame()
            self.frames += 1
            if keyframe:
                self.keyframes += 1
                self._seen_keyframe = True
            self._video_frame_is_keyframe = keyframe
            self._video_frame_recv_ms = time.monotonic() * 1000.0

        # Everything before the first keyframe references a picture ffmpeg has
        # never seen, so it is noise at best.
        if not self._seen_keyframe:
            return
        self._video_frame_buf += data

    def _finalize_video_frame(self) -> None:
        """Queue whatever has been accumulated in _video_frame_buf as one
        complete access unit. Called when the next frame starts - the
        small, final in-progress frame at teardown is not flushed, and is
        lost; harmless, since ffmpeg is being torn down at that point
        regardless.
        """
        if not self._video_frame_buf:
            return
        frame_bytes = bytes(self._video_frame_buf)
        self._video_frame_buf.clear()
        item = (frame_bytes, self._video_frame_is_keyframe, self._video_frame_recv_ms)
        try:
            self._video_queue.put_nowait(item)
        except queue.Full:
            # Backlog beyond the cap - drop the oldest rather than let
            # latency creep up unbounded.
            with contextlib.suppress(queue.Empty):
                self._video_queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._video_queue.put_nowait(item)

    def feed_audio(self, data: bytes, header: bytes | None) -> None:
        """Called from the session's receive path. Must not block.

        header is the frame's 27-byte per-frame sub-header (present on a
        frame-start chunk, None on a continuation - mirrors feed_video).
        Every mu-law frame is a fixed 320 bytes and so far always arrives
        as a single payload with its own header, but the continuation case
        is handled the same defensive way as video rather than assumed
        away.

        The queued pts is decoded from the device's own embedded clock
        (decode_audio_clock_ms) - a genuine absolute Unix-seconds field
        plus an exact +40ms/frame sub-second field, confirmed reliable to
        the ms against the real device (zero monotonic violations across
        398 real, properly-ordered frames). Unlike video, no
        disambiguation logic is needed here at all.
        """
        if not self._enable_audio:
            return
        if header is not None:
            self._finalize_audio_frame()
            self._audio_frame_recv_ms = decode_audio_clock_ms(header)
        self._audio_frame_buf += data

    def _finalize_audio_frame(self) -> None:
        """Queue whatever has been accumulated in _audio_frame_buf as one
        complete frame. Mirrors _finalize_video_frame.
        """
        if not self._audio_frame_buf:
            return
        chunk = bytes(self._audio_frame_buf)
        self._audio_frame_buf.clear()
        item = (chunk, self._audio_frame_recv_ms)
        try:
            self._audio_queue.put_nowait(item)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._audio_queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._audio_queue.put_nowait(item)

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
        except Exception:
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
        except Exception:
            _LOGGER.debug("ffmpeg stderr reader stopped", exc_info=True)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()
