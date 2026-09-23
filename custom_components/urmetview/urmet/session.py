"""Asyncio session against an Urmet intercom.

One :class:`UrmetSession` owns exactly one UDP socket and the device's single
session slot. It is deliberately free of Home Assistant imports so it can be
exercised from a plain script (see ``tools/urmet_cli.py``).

Two background obligations run for the lifetime of the session and have no
command/response visibility, so they are the easiest things to omit and the
hardest to debug (protocol doc sections 4b-2 and 6b):

* a ping every ~1.2s, and
* a ``d1`` ack for **every** received ``d0`` packet, including retransmits.

Without the acks the device never advances its send window: it retransmits one
frame forever and encodes nothing new, which presents as "video works for two
seconds then freezes" while every command still returns ``{"result":"ok"}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from . import protocol as p
from .const import (
    ACK_REPEAT,
    COMMAND_RETRIES,
    COMMAND_TIMEOUT,
    LOGIN_TIMEOUT,
    MAX_PENDING_PACKETS,
    PING_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

VideoCallback = Callable[[bytes, bool, bool], None]
"""``(data, is_frame_start, is_keyframe)``."""

AudioCallback = Callable[[bytes], None]


class UrmetError(Exception):
    """Base class for session failures."""


class UrmetAuthError(UrmetError):
    """The device rejected our credentials."""


class UrmetBusyError(UrmetError):
    """Another session holds the resource we asked for."""


class UrmetTimeoutError(UrmetError):
    """The device did not answer in time."""


class _ChannelReassembler:
    """In-order delivery for one channel.

    Media is a byte stream, so a payload processed out of order splices
    unrelated bytes into the middle of an H.264 frame - which decodes as
    "Failed to parse header of NALU (type 0)" even though frame boundaries are
    correct. Tracking a next-expected sequence makes duplicates, reordering and
    gaps all fall out of one mechanism; a content-based or fixed-window dedup
    does not (a retransmit evicted from the window gets re-processed as new
    data).
    """

    def __init__(self) -> None:
        self._next_seq: int | None = None
        self._pending: dict[int, bytes] = {}

    def push(self, seq: int, payload: bytes) -> list[bytes]:
        """Feed one packet, get back whatever is now deliverable in order."""
        if self._next_seq is None:
            self._next_seq = seq

        # 16-bit sequence: a large positive delta is really a wrapped negative
        # one, i.e. a packet older than what we already emitted.
        if (seq - self._next_seq) & 0xFFFF > 0x8000:
            return []

        self._pending[seq] = payload
        ready = self._drain()

        if len(self._pending) > MAX_PENDING_PACKETS:
            oldest = min(self._pending)
            _LOGGER.debug(
                "Sequence gap: packet %s never arrived, skipping to %s",
                self._next_seq,
                oldest,
            )
            self._next_seq = oldest
            ready.extend(self._drain())
        return ready

    def _drain(self) -> list[bytes]:
        ready: list[bytes] = []
        assert self._next_seq is not None
        while self._next_seq in self._pending:
            ready.append(self._pending.pop(self._next_seq))
            self._next_seq = (self._next_seq + 1) & 0xFFFF
        return ready

    @property
    def held(self) -> int:
        return len(self._pending)


class UrmetSession(asyncio.DatagramProtocol):
    """A live, logged-in session with the device."""

    def __init__(
        self,
        host: str,
        port: int,
        uid: str,
        auth_hash: str,
        username: str = "admin",
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.uid = uid
        self.auth_hash = auth_hash
        self.username = username

        self._loop = loop or asyncio.get_running_loop()
        self._transport: asyncio.DatagramTransport | None = None
        self._closed = asyncio.Event()

        self._cmd_seq = 0
        self._out_seq = {p.CHANNEL_COMMAND: 0, p.CHANNEL_MEDIA: 0}
        self._reassemblers: dict[int, _ChannelReassembler] = {}
        self._cmd_buffer = b""

        self._pending: dict[int, deque[asyncio.Future[p.CommandResponse]]] = {}
        self._ping_task: asyncio.Task[None] | None = None

        self.device_info: dict[str, Any] = {}
        self.current_unit: int | None = None
        self.last_media_at: float = 0.0
        self.video_frames = 0
        self.audio_frames = 0

        self.on_video: VideoCallback | None = None
        self.on_audio: AudioCallback | None = None

        #: Observation hooks. ``on_command`` sees every decoded command block,
        #: including ones nothing is waiting for - which is how an unsolicited
        #: message such as a doorbell ring would first show up. ``on_raw`` sees
        #: every inbound datagram before any parsing.
        self.on_command: Callable[[p.CommandResponse], None] | None = None
        self.on_raw: Callable[[bytes], None] | None = None

        self._current_stream_is_video = False
        self._connected = False
        self._unreachable: Exception | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected and self._transport is not None

    async def async_connect(self) -> None:
        """Open the socket, complete the handshake and log in."""
        transport, _ = await self._loop.create_datagram_endpoint(
            lambda: self, remote_addr=(self.host, self.port)
        )
        self._transport = transport  # type: ignore[assignment]
        self._connected = True
        try:
            await self._async_handshake()
            await self._async_login()
        except Exception:
            await self.async_close(graceful=False)
            raise
        self._ping_task = self._loop.create_task(self._async_ping_loop())

    async def _async_handshake(self) -> None:
        """checkCam + ping. The device answers with 0x42/0xe1, but we do not
        block on those - login is the real readiness signal."""
        checkcam = p.build_simple(p.MSG_CHECKCAM, p.pack_uid_short(self.uid))
        for _ in range(4):
            self._send(checkcam)
        self._send(p.build_simple(p.MSG_PING))
        await asyncio.sleep(0.3)

    async def _async_login(self) -> None:
        """Send the bundled hello + credentials exactly as the app does."""
        blocks = self._block(p.SUBCMD_HELLO, "{}") + self._block(
            p.SUBCMD_LOGIN,
            '{"username":"%s","auth":"%s"}' % (self.username, self.auth_hash),
        )
        try:
            response = await self._async_send_blocks_wait(
                blocks, p.SUBCMD_LOGIN, timeout=LOGIN_TIMEOUT
            )
        except UrmetTimeoutError as err:
            raise UrmetTimeoutError(
                "No login response - check the host/port (the session port "
                "changes per session) and that the device is reachable"
            ) from err

        if response.data.get("auth") != "ok":
            raise UrmetAuthError(
                f"Device rejected the auth hash: {response.text!r}. The hash is "
                "captured from a real app login and is device-specific."
            )
        _LOGGER.debug("Logged in to %s:%s", self.host, self.port)

    async def async_query_device_info(self) -> None:
        """Replicate the app's post-login info requests.

        These are ``GET /path`` strings in normal command framing and answer
        with XML, not JSON. Fidelity with the app only - not required, and
        failures here are not fatal.
        """
        blocks = (
            self._block(p.SUBCMD_HELLO, "{}")
            + self._block(p.SUBCMD_GET_INFO, "GET /Network/P2PV2")
            + self._block(p.SUBCMD_GET_INFO, "GET /System/DeviceCap")
        )
        self._send_data(blocks)
        await asyncio.sleep(0.2)

    async def async_close(self, graceful: bool = True) -> None:
        """Tear down the session the way the app does, then close the socket.

        Skipping the teardown leaves the video channel held until the device's
        own timeout, so the *next* connect is refused with ``video busy``.
        This runs on every exit path, including cancellation.
        """
        if self._ping_task is not None:
            self._ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ping_task
            self._ping_task = None

        if graceful and self._transport is not None:
            blocks = (
                self._block(p.SUBCMD_TALK_ACTION, '{"action":"stop"}')
                + self._block(p.SUBCMD_TALK_CHANNEL_OFF, '{"channel":"1"}')
                + self._block(p.SUBCMD_AUDIO_STOP, '{"channel":"0"}')
                + self._block(p.SUBCMD_VIDEO_STOP, '{"channel":"0"}')
            )
            with contextlib.suppress(OSError):
                self._send_data(blocks, repeat=4)
            # There is no ack to wait for; give the datagrams a moment to leave.
            await asyncio.sleep(0.2)

        self._connected = False
        if self._transport is not None:
            self._transport.close()
            self._transport = None

        for futures in self._pending.values():
            for future in futures:
                if not future.done():
                    future.set_exception(UrmetError("Session closed"))
        self._pending.clear()
        self._closed.set()

    # -- sending ------------------------------------------------------------

    def _send(self, data: bytes) -> None:
        if self._transport is None:
            raise UrmetError("Session is not connected")
        self._transport.sendto(data)

    def _next_cmd_seq(self) -> int:
        seq = self._cmd_seq
        self._cmd_seq = (self._cmd_seq + 1) & 0xFFFF
        return seq

    def _block(self, subcmd: int, text: str) -> bytes:
        return p.build_command_block(subcmd, text, self._next_cmd_seq())

    def _trigger_block(self, subcmd: int) -> bytes:
        return p.build_trigger_block(subcmd, self._next_cmd_seq())

    def _send_data(
        self, payload: bytes, channel: int = p.CHANNEL_COMMAND, repeat: int = 3
    ) -> bytes:
        """Send one ``d0`` frame, transmitted ``repeat`` times.

        Redundancy is at the datagram level with an unchanged sequence number,
        which is what the app does and what the device's dedup expects. This
        matters for triggers: re-*building* a unit-select command would toggle
        the unit a second time, while re-*sending* the same datagram is a
        no-op the device discards.
        """
        seq = self._out_seq[channel]
        self._out_seq[channel] = (seq + 1) & 0xFFFF
        frame = p.build_data(channel, seq, payload)
        for _ in range(repeat):
            self._send(frame)
        return frame

    async def _async_send_blocks_wait(
        self,
        blocks: bytes,
        expect_subcmd: int,
        timeout: float = COMMAND_TIMEOUT,
        retries: int = COMMAND_RETRIES,
    ) -> p.CommandResponse:
        """Send command blocks and wait for the device's reply.

        Retries re-send the identical datagram rather than rebuilding it, so
        this is safe for the parameterless triggers where a second logical
        command would have a real-world side effect.
        """
        frame: bytes | None = None
        for attempt in range(retries):
            if self._unreachable is not None:
                raise UrmetError(
                    f"Nothing is listening on {self.host}:{self.port} - "
                    "the device's session port has changed"
                )
            future: asyncio.Future[p.CommandResponse] = self._loop.create_future()
            self._pending.setdefault(expect_subcmd, deque()).append(future)
            try:
                if frame is None:
                    frame = self._send_data(blocks)
                else:
                    for _ in range(3):
                        self._send(frame)
                return await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "No response to subcmd 0x%04x (attempt %s/%s)",
                    expect_subcmd,
                    attempt + 1,
                    retries,
                )
            finally:
                queue = self._pending.get(expect_subcmd)
                if queue is not None and future in queue:
                    queue.remove(future)
        raise UrmetTimeoutError(f"No response to subcmd 0x{expect_subcmd:04x}")

    async def async_send_command(
        self, subcmd: int, text: str, timeout: float = COMMAND_TIMEOUT
    ) -> p.CommandResponse:
        return await self._async_send_blocks_wait(
            self._block(subcmd, text), subcmd, timeout=timeout
        )

    async def async_send_trigger(
        self, subcmd: int, timeout: float = COMMAND_TIMEOUT
    ) -> p.CommandResponse:
        return await self._async_send_blocks_wait(
            self._trigger_block(subcmd), subcmd, timeout=timeout
        )

    def send_talk_frame(self, pcmu: bytes, repeat: int = 6) -> None:
        """Push one 40ms mu-law frame to the device's speaker."""
        self._send_data(
            p.build_audio_out_frame(pcmu), channel=p.CHANNEL_MEDIA, repeat=repeat
        )

    # -- receiving ----------------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.on_raw is not None:
            try:
                self.on_raw(data)
            except Exception:  # noqa: BLE001 - an observer must never break the session
                _LOGGER.exception("on_raw hook raised")

        packet = p.parse_packet(data)
        if packet is None:
            return

        # Ack first, before dedup: a duplicate means our previous ack was lost
        # or not yet processed, so it needs acking again even though we will
        # discard the payload. Acking must also never be starved by downstream
        # frame processing, which is why it happens here at the top.
        ack = p.build_ack(packet.channel, [packet.seq])
        if ack is not None:
            try:
                for _ in range(ACK_REPEAT):
                    self._send(ack)
            except (OSError, UrmetError):
                _LOGGER.debug("Ack send failed", exc_info=True)

        reassembler = self._reassemblers.setdefault(
            packet.channel, _ChannelReassembler()
        )
        for payload in reassembler.push(packet.seq, packet.payload):
            try:
                self._process_payload(payload)
            except Exception:  # noqa: BLE001 - one bad payload must not kill the socket
                _LOGGER.exception(
                    "Error processing payload on channel %s", packet.channel
                )

    def _process_payload(self, payload: bytes) -> None:
        """Route one in-order payload.

        Classification is purely marker-based. Size heuristics misroute the
        small trailing continuation chunk of a frame as if it were a command
        response.
        """
        media = p.parse_media_start(payload)
        if media is not None:
            self._handle_media_start(media)
            return

        if payload[0:4] == p.MARKER_CMD:
            self._current_stream_is_video = False
            self._cmd_buffer += payload
            self._flush_command_buffer()
            return

        # No marker: a continuation of whichever stream was last opened.
        if self._current_stream_is_video:
            self.last_media_at = time.monotonic()
            if self.on_video is not None:
                self.on_video(payload, False, False)
        elif self._cmd_buffer:
            self._cmd_buffer += payload
            self._flush_command_buffer()
        elif self.on_audio is not None:
            self.last_media_at = time.monotonic()
            self.on_audio(payload)

    def _handle_media_start(self, media: p.MediaFrameStart) -> None:
        self.last_media_at = time.monotonic()
        self._current_stream_is_video = media.is_video
        if media.is_video:
            # 0x01 (SPS/PPS/IDR) and 0x02 (P-frames) are two halves of one
            # ordinary H.264 GOP and must be merged into a single elementary
            # stream. Forwarding only 0x02 yields "non-existing PPS 0
            # referenced" and no picture.
            self.video_frames += 1
            if self.on_video is not None:
                self.on_video(media.data, True, media.is_keyframe)
        elif media.is_audio:
            self.audio_frames += 1
            if self.on_audio is not None:
                self.on_audio(media.data)

    def _flush_command_buffer(self) -> None:
        responses, self._cmd_buffer = p.parse_command_blocks(self._cmd_buffer)
        for response in responses:
            self._dispatch_response(response)

    def _dispatch_response(self, response: p.CommandResponse) -> None:
        if self.on_command is not None:
            try:
                self.on_command(response)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("on_command hook raised")

        data = response.data
        if response.subcmd == p.SUBCMD_HELLO and data:
            # Sent repeatedly for the whole session as an announce/keepalive.
            self.device_info.update(data)
        if response.subcmd == p.SUBCMD_SELECT_UNIT and "door" in data:
            try:
                self.current_unit = int(data["door"])
            except (TypeError, ValueError):
                pass

        queue = self._pending.get(response.subcmd)
        while queue:
            future = queue.popleft()
            if not future.done():
                future.set_result(response)
                return

    def error_received(self, exc: Exception) -> None:
        """ICMP errors surface here on a connected UDP socket.

        Connection-refused means an ICMP port-unreachable came back: nothing is
        listening on that port, so every retry will fail the same way. Give up
        at once and fail anything waiting, rather than spending the full retry
        budget - three eight-second timeouts - discovering it slowly.
        """
        _LOGGER.debug("UDP error on session to %s:%s: %s", self.host, self.port, exc)
        if isinstance(exc, ConnectionRefusedError):
            self._unreachable = exc
            # Without this, `connected` (self._connected and transport is
            # not None) stays True forever - the transport is never told
            # its socket died, so _async_keepalive's "session down,
            # reconnect" check never fires for this failure mode even
            # though nothing is listening on the port any more.
            self._connected = False
            for futures in self._pending.values():
                for future in futures:
                    if not future.done():
                        future.set_exception(
                            UrmetError(
                                f"Nothing is listening on {self.host}:{self.port} - "
                                "the device's session port has changed"
                            )
                        )

    def connection_lost(self, exc: Exception | None) -> None:
        self._connected = False
        self._closed.set()

    # -- background ---------------------------------------------------------

    async def _async_ping_loop(self) -> None:
        """A plain heartbeat, unrelated to anything the user does.

        Pinging only at session-open makes media delivery degrade, with no
        error anywhere to point at it.
        """
        ping = p.build_simple(p.MSG_PING)
        while self._connected:
            try:
                self._send(ping)
            except (OSError, UrmetError) as err:
                _LOGGER.debug("Ping failed: %s", err)
            await asyncio.sleep(PING_INTERVAL)

    # -- high level commands ------------------------------------------------

    async def async_start_video(self, quality: str) -> None:
        """Start the video stream.

        A refusal here is easy to miss because everything else keeps working:
        login succeeds and unit/key/gate are all accepted, only media never
        arrives. So the result is checked rather than assumed.
        """
        value = p.QUALITY_VALUES[quality]
        response = await self.async_send_command(
            p.SUBCMD_VIDEO_START,
            '{"channel":"0","quality":"%s","type":"1"}' % value,
        )
        result = response.data.get("result", "")
        if result == "ok":
            return
        if "busy" in result:
            raise UrmetBusyError(
                "The device refused to start video (video busy): another "
                "session still holds the video channel. Close the UrmetView "
                "app, wait ~30-60s for the stale session to time out, then "
                "retry."
            )
        raise UrmetError(f"start_video failed: {response.text!r}")

    async def async_stop_video(self) -> None:
        with contextlib.suppress(UrmetError):
            await self.async_send_command(p.SUBCMD_VIDEO_STOP, '{"channel":"0"}')

    async def async_start_audio(self) -> None:
        with contextlib.suppress(UrmetError):
            await self.async_send_command(p.SUBCMD_AUDIO_START, '{"channel":"0"}')

    async def async_stop_audio(self) -> None:
        with contextlib.suppress(UrmetError):
            await self.async_send_command(p.SUBCMD_AUDIO_STOP, '{"channel":"0"}')

    async def async_set_quality(self, quality: str) -> None:
        """Change bitrate/frame rate mid-stream.

        This does not change resolution - the stream is 960x240 in every mode.
        The official app behaves identically, so a client that sees no
        resolution change is working correctly.
        """
        value = p.QUALITY_VALUES[quality]
        nonce = str(int(time.time() * 1000))
        await self.async_send_command(
            p.SUBCMD_STREAM_CONFIG,
            '{"channel":"0","video":{"codec":"unknown","quality":"%s"},'
            '"audio":{"codec":"unknown","freq":"1","sample":"%s","channel":"1"}}'
            % (value, nonce),
        )

    async def async_cycle_unit(self) -> int | None:
        """Advance to the next unit and return the unit now selected.

        The ``d2 07`` command takes no parameter - it cycles - so selecting a
        specific unit means calling this until the reported ``door`` matches.
        See :meth:`UrmetCoordinator.async_select_unit`.
        """
        response = await self.async_send_trigger(p.SUBCMD_SELECT_UNIT)
        result = response.data.get("result", "")
        if result == "busy":
            raise UrmetBusyError(
                "Unit selection returned busy - video must be started first"
            )
        door = response.data.get("door")
        if door is None:
            return None
        try:
            self.current_unit = int(door)
        except (TypeError, ValueError):
            return None
        return self.current_unit

    async def async_trigger_key(self) -> None:
        """Pulse the electric lock on the currently selected unit."""
        response = await self.async_send_trigger(p.SUBCMD_KEY)
        self._check_trigger(response, "key")

    async def async_trigger_gate(self) -> None:
        """Pulse the gate relay on the currently selected unit."""
        response = await self.async_send_trigger(p.SUBCMD_GATE)
        self._check_trigger(response, "gate")

    @staticmethod
    def _check_trigger(response: p.CommandResponse, name: str) -> None:
        result = response.data.get("result", "")
        if result == "ok":
            return
        if "busy" in result:
            raise UrmetBusyError(
                f"{name} returned busy - video must be started before "
                "unit/key/gate commands are accepted"
            )
        raise UrmetError(f"{name} failed: {response.text!r}")

    async def async_talk_start(self) -> None:
        """Open the outbound audio path.

        ``channel:"1"`` is a fixed audio-subsystem index, not the unit
        selector - it stays "1" while unit 2 is selected.
        """
        await self.async_send_command(p.SUBCMD_TALK_CHANNEL_ON, '{"channel":"1"}')
        await self.async_send_command(p.SUBCMD_TALK_ACTION, '{"action":"start"}')

    async def async_talk_stop(self) -> None:
        with contextlib.suppress(UrmetError):
            await self.async_send_command(p.SUBCMD_TALK_ACTION, '{"action":"stop"}')
        with contextlib.suppress(UrmetError):
            await self.async_send_command(p.SUBCMD_TALK_CHANNEL_OFF, '{"channel":"0"}')
