"""Owns the device session and arbitrates access to it.

The device serves one client at a time and shows one outdoor station at a time,
so everything funnels through here.

The session model matters: we hold a **command session** (login + ping + acks)
for the lifetime of the config entry, but start **video only on demand**. That
keeps the video channel free so the phone app still works, and means we are not
holding a resource nobody is looking at. Video is released again shortly after
the last viewer goes away.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DEFAULT_PIXEL_ASPECT,
    DEFAULT_STREAM_IDLE_TIMEOUT,
    DEFAULT_TZSP_PORT,
    SIGNAL_STATE_UPDATED,
)
from . import doorbell as doorbell_mirror
from .media import MediaPipeline
from .urmet import UrmetAuthError, UrmetError, UrmetSession
from .urmet import audio as urmet_audio
from .urmet import discovery
from .urmet.const import DEFAULT_QUALITY, DEFAULT_TALK_REPEAT

_LOGGER = logging.getLogger(__name__)

#: Unit selection cycles rather than selects, so reaching a specific station
#: means repeating. Bounded so an unexpected reply cannot spin forever.
MAX_CYCLE_ATTEMPTS = 4

RECONNECT_BACKOFF = (2, 5, 10, 30, 60)

#: How long without a real video/audio frame before a "running" stream is
#: treated as stale rather than trusted. Generous against real observed
#: gaps (well under 1s for video, a couple of seconds for audio even under
#: jitter) - this is about catching the device having gone quiet, not
#: normal delivery variance.
STREAM_STALE_TIMEOUT = 10.0

#: How long to give a cheap resume (re-sending start_video/start_audio on
#: the SAME, still-connected session) before deciding it did not work and
#: falling back to a full reconnect. Confirmed against the real device:
#: media resumed within a few seconds of re-sending these on an already-open
#: session, no re-login needed, when the session itself was still alive and
#: it was specifically the device's stream that had gone quiet.
CHEAP_RESUME_GRACE = 8.0


class UrmetCoordinator:
    """Single owner of the device session."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        uid: str,
        auth_hash: str,
        username: str,
        ffmpeg_binary: str,
        host: str | None = None,
        port: int | None = None,
        quality: str = DEFAULT_QUALITY,
        stream_idle_timeout: int = DEFAULT_STREAM_IDLE_TIMEOUT,
        talk_repeat: int = DEFAULT_TALK_REPEAT,
        doorbell_mirror: bool = False,
        doorbell_port: int = DEFAULT_TZSP_PORT,
        ring_prewarm: bool = False,
        allow_cloud: bool = True,
        pixel_aspect: str = DEFAULT_PIXEL_ASPECT,
        stream_audio: bool = True,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.uid = uid
        self.auth_hash = auth_hash
        self.username = username
        self.host = host
        self.port = port
        self.quality = quality
        self.stream_idle_timeout = stream_idle_timeout
        self.talk_repeat = talk_repeat
        self.doorbell_mirror = doorbell_mirror
        self.doorbell_port = doorbell_port
        self.ring_prewarm = ring_prewarm
        self.allow_cloud = allow_cloud

        self.session: UrmetSession | None = None
        self.pipeline = MediaPipeline(
            ffmpeg_binary,
            enable_audio=stream_audio,
            pixel_aspect=pixel_aspect,
        )

        self.available = False
        self.station: int | None = None
        self.talking = False
        self.last_error: str | None = None

        self._command_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        # Serialises start/stop/restart of the device stream. Without it a
        # stream_source() arriving during a restart's stop-then-start window
        # sees _video_running False and issues a second start_video, and the
        # device answers the loser 'video busy'.
        self._video_lock = asyncio.Lock()
        self._restarting = False
        self._video_running = False
        self._last_activity = 0.0
        self._idle_task: asyncio.Task[None] | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._shutdown = False
        self._doorbell_transport = None
        self._doorbell_listener = None
        self._doorbell_callbacks: list[Callable[[], None]] = []

    # -- lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        """Connect, and keep reconnecting for the entry's lifetime."""
        await self._async_connect()
        self._connect_task = self.hass.async_create_background_task(
            self._async_keepalive(), name=f"urmetview-keepalive-{self.entry_id}"
        )
        if self.doorbell_mirror:
            await self._async_start_doorbell()

    async def _async_start_doorbell(self) -> None:
        """Listen for mirrored traffic so rings become local events.

        Optional, and a failure here must not take the whole integration down -
        video and the door controls work fine without it.
        """
        try:
            (
                self._doorbell_transport,
                self._doorbell_listener,
            ) = await doorbell_mirror.async_start_listener(
                self.doorbell_port,
                self.host,
                self._fire_doorbell,
                self._note_register_port,
            )
        except OSError as err:
            _LOGGER.error(
                "Could not bind the doorbell mirror port %s (%s). Ring detection "
                "is disabled; everything else still works.",
                self.doorbell_port,
                err,
            )

    @callback
    def register_doorbell_callback(self, callback_fn: Callable[[], None]) -> None:
        self._doorbell_callbacks.append(callback_fn)

    @callback
    def _fire_doorbell(self) -> None:
        _LOGGER.debug("Doorbell rang")
        for callback_fn in self._doorbell_callbacks:
            callback_fn()
        if self.ring_prewarm:
            self.hass.async_create_task(self._async_prewarm())

    async def _async_prewarm(self) -> None:
        """Start the stream on a ring so a picture is ready sooner.

        The device does not push video when the bell rings - captures with the
        app closed contain no media at all. Video is pull-only, so a snapshot
        of whoever is at the door does not exist until someone asks for it,
        and then costs ~1s to start plus up to ~5s waiting for a keyframe.

        Off by default, because it takes the device's single video channel at
        exactly the moment the phone app wants it to answer the call. Only
        worth enabling if Home Assistant has replaced the app for you.
        """
        try:
            await self._async_start_video()
        except UrmetError as err:
            _LOGGER.debug("Ring pre-warm could not start video: %s", err)

    @callback
    def _note_register_port(self, port: int) -> None:
        """The device registers from the port it serves sessions on.

        Free, always-current port discovery for anyone mirroring traffic - it
        removes the need to rediscover after the device rotates its port.
        """
        if port != self.port:
            _LOGGER.debug("Device registration port changed to %s", port)
            self.port = port

    async def async_shutdown(self) -> None:
        self._shutdown = True
        if self._connect_task is not None:
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._cancel_idle_timer()
        if self._doorbell_transport is not None:
            self._doorbell_transport.close()
            self._doorbell_transport = None
        await self.pipeline.async_shutdown()
        await self._async_disconnect()

    async def _async_connect(self) -> None:
        """Find the device and log in, trying each candidate in turn.

        A login is the only meaningful verification. The device answers LAN
        search by opening a one-shot session offer, and that offer binds to the
        first peer that talks to it - so probing a candidate before connecting
        claims it, and the real session is then ignored. Discovery therefore
        hands back guesses, and connecting is what tests them.
        """
        async with self._connect_lock:
            if self.session is not None and self.session.connected:
                return

            candidates = await discovery.async_find_candidates(
                self.uid,
                host=self.host,
                cached_port=self.port,
                allow_cloud=self.allow_cloud,
                # Once the host is known the sweep is a fully local fallback,
                # which matters on a reconnect after the port has changed.
                allow_sweep=bool(self.host),
            )
            if not candidates:
                raise UrmetError(
                    "Could not locate the intercom. Check it is powered and on "
                    "the same network, or set a static host/port in the options."
                )

            last_error: UrmetError | None = None
            for candidate in candidates:
                try:
                    await self._async_connect_to(candidate.host, candidate.port)
                except UrmetAuthError:
                    raise  # a bad hash will not fix itself on another port
                except UrmetError as err:
                    _LOGGER.debug("No session at %s: %s", candidate, err)
                    last_error = err
                    continue
                return

            raise last_error or UrmetError("No candidate accepted a session")

    async def _async_connect_to(self, host: str, port: int) -> None:
        """Log in at one address, and adopt it as ours if it works."""
        session = UrmetSession(host, port, self.uid, self.auth_hash, self.username)
        session.on_video = self._on_video
        session.on_audio = self._on_audio
        await session.async_connect()
        with contextlib.suppress(UrmetError):
            await session.async_query_device_info()

        self.session = session
        self.host, self.port = host, port
        self.available = True
        self.last_error = None
        _LOGGER.info("Connected to Urmet intercom at %s:%s", host, port)
        self._notify()

    async def _async_disconnect(self) -> None:
        session, self.session = self.session, None
        self.available = False
        self._video_running = False
        if session is not None:
            with contextlib.suppress(Exception):
                await session.async_close()

    async def _async_keepalive(self) -> None:
        """Rebuild the session whenever it drops."""
        attempt = 0
        while not self._shutdown:
            await asyncio.sleep(5)
            if self.session is not None and self.session.connected:
                attempt = 0
                continue
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            _LOGGER.debug("Session down, reconnecting in %ss", delay)
            await asyncio.sleep(delay)
            if self._shutdown:
                return
            try:
                await self._async_disconnect()
                await self._async_connect()
                # A viewer may still be attached to the stream we just lost.
                if self.pipeline.client_count:
                    await self._async_start_video()
            except UrmetError as err:
                self.last_error = str(err)
                _LOGGER.debug("Reconnect failed: %s", err)
                self._notify()

    # -- media callbacks (hot path - must not block) ------------------------

    @callback
    def _on_video(self, data: bytes, frame_start: bool, keyframe: bool) -> None:
        self.pipeline.feed_video(data, frame_start, keyframe)

    @callback
    def _on_audio(self, data: bytes) -> None:
        self.pipeline.feed_audio(data)

    # -- video on demand ----------------------------------------------------

    async def async_ensure_stream(self, *, mark_activity: bool = True) -> str:
        """Bring the device stream up and return the local stream URL.

        Deliberately not reference counted. Home Assistant calls
        ``stream_source()`` without a matching "done" callback, so a counter
        would only ever increase and the video channel would stay pinned open.
        Instead the idle monitor watches the muxed output's real TCP consumers,
        which cannot drift out of sync with reality.

        ``mark_activity=False`` is for a snapshot grab (see camera.py), not a
        real "someone is watching" signal - without this, a periodically
        refreshed dashboard thumbnail keeps calling this on every poll and
        each call was unconditionally resetting the idle clock, so the device
        stream never actually went idle even with nobody's live view open.
        Still marked when video was not already running, though: a snapshot
        can be the thing that starts it fresh, and that grace period is what
        stops the idle monitor tearing it back down in the gap before the
        snapshot's own ffmpeg process has connected as a real consumer.
        """
        was_running = self._video_running
        await self._async_start_video()
        if mark_activity or not was_running:
            self._mark_activity()
        return self.pipeline.stream_url

    @callback
    def _mark_activity(self) -> None:
        """Note that something wants the stream, even if not yet connected.

        A consumer takes a moment to connect after asking for the URL; without
        this grace period the idle monitor could tear the stream down in the
        gap between the two.
        """
        self._last_activity = time.monotonic()

    async def _async_start_video(self) -> None:
        async with self._video_lock:
            await self._async_start_video_locked()

    async def _async_start_video_locked(self) -> None:
        """Caller must hold _video_lock."""
        if self._video_running:
            if self._stream_is_healthy():
                return
            # The session can stay "connected" at the socket level while the
            # device has silently stopped sending - confirmed on the real
            # device (also reproduced by the Urmet phone app itself, so this
            # is device behaviour to design around, not a bug to chase
            # further). A consumer asking for the stream again is exactly
            # the moment to notice and recover, rather than handing back the
            # same dead pipeline.
            _LOGGER.debug(
                "Stream requested again but no media for >%ss - recovering",
                STREAM_STALE_TIMEOUT,
            )
            await self._async_recover_stale_stream()
            return
        session = await self._async_require_session()
        await self.pipeline.async_start()
        try:
            async with self._command_lock:
                await session.async_start_video(self.quality)
                with contextlib.suppress(UrmetError):
                    await session.async_start_audio()
        except Exception:
            # The device refused - "video busy" when the phone app holds the
            # channel, most often. ffmpeg is already up, and the idle monitor
            # that would normally clean it up is only started on success, so
            # without this the process is left running until the entry is
            # unloaded.
            await self.pipeline.async_stop()
            raise
        self._video_running = True
        self._mark_activity()
        self._start_idle_monitor()
        _LOGGER.debug("Device video started")
        self._notify()

    def _stream_is_healthy(self) -> bool:
        """A running stream is only trustworthy if media is actually
        arriving - ``session.connected`` alone is not enough evidence, it is
        a transport/socket-level flag that can stay True while the device
        has gone quiet."""
        session = self.session
        if session is None or not session.connected:
            return False
        return time.monotonic() - session.last_media_at < STREAM_STALE_TIMEOUT

    async def _async_recover_stale_stream(self) -> None:
        """Caller must hold _video_lock, with ``_video_running`` already True.

        Tries the cheap path first: re-send start_video/start_audio on the
        SAME, still-connected session. Confirmed against the real device
        this alone resumes a stalled stream within a few seconds, no
        re-login needed - the common case is the session surviving while
        specifically the device's stream went quiet. Only falls back to a
        full reconnect (the same path _async_keepalive uses for a session
        that dropped outright) if the cheap path does not bring media back
        within CHEAP_RESUME_GRACE.
        """
        session = self.session
        if session is not None and session.connected:
            with contextlib.suppress(UrmetError):
                async with self._command_lock:
                    await session.async_start_video(self.quality)
                    await session.async_start_audio()
            deadline = time.monotonic() + CHEAP_RESUME_GRACE
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                if time.monotonic() - session.last_media_at < CHEAP_RESUME_GRACE:
                    _LOGGER.debug("Stream recovered via cheap resume")
                    self._mark_activity()
                    return
            _LOGGER.debug(
                "Cheap resume did not bring media back within %ss - "
                "falling back to a full reconnect",
                CHEAP_RESUME_GRACE,
            )

        await self._async_disconnect()
        await self._async_connect()
        session = await self._async_require_session()
        async with self._command_lock:
            await session.async_start_video(self.quality)
            with contextlib.suppress(UrmetError):
                await session.async_start_audio()
        self._mark_activity()
        _LOGGER.debug("Stream recovered via full reconnect")

    async def _async_stop_video(self) -> None:
        async with self._video_lock:
            await self._async_stop_video_locked()

    async def _async_stop_video_locked(self) -> None:
        """Caller must hold _video_lock."""
        if not self._video_running:
            return
        self._video_running = False
        session = self.session
        if session is not None and session.connected:
            async with self._command_lock:
                await session.async_stop_audio()
                await session.async_stop_video()
        await self.pipeline.async_stop()
        _LOGGER.debug("Device video released")
        self._notify()

    def _start_idle_monitor(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = self.hass.async_create_background_task(
            self._async_idle_monitor(), name=f"urmetview-idle-{self.entry_id}"
        )

    async def _async_idle_monitor(self) -> None:
        """Release the device stream once nobody is actually consuming it.

        Watching real TCP consumers rather than a counter means an abandoned
        stream, a crashed viewer or a failed snapshot all resolve on their own.
        """
        try:
            while self._video_running and not self._shutdown:
                await asyncio.sleep(2.0)
                if self.pipeline.client_count > 0:
                    self._mark_activity()
                    if not self._stream_is_healthy():
                        # Someone is actively watching a stream that has
                        # gone quiet - recover now rather than waiting for
                        # them to notice and ask again.
                        with contextlib.suppress(UrmetError):
                            await self._async_start_video()
                    continue
                if time.monotonic() - self._last_activity < self.stream_idle_timeout:
                    continue
                _LOGGER.debug(
                    "No stream consumers for %ss, releasing video",
                    self.stream_idle_timeout,
                )
                with contextlib.suppress(UrmetError):
                    await self._async_stop_video()
                return
        except asyncio.CancelledError:
            raise

    def _cancel_idle_timer(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    async def _async_require_session(self) -> UrmetSession:
        if self.session is None or not self.session.connected:
            await self._async_connect()
        if self.session is None:
            raise UrmetError("Not connected to the intercom")
        return self.session

    async def _async_with_video(self):
        """Ensure a video session exists, because commands need one.

        Station/lock/gate are refused with 'busy' unless video is running, so a
        command issued while nothing is watching briefly brings the stream up.
        """
        session = await self._async_require_session()
        if not self._video_running:
            await self._async_start_video()
        return session

    # -- actions ------------------------------------------------------------

    async def async_select_station(self, target: int) -> bool:
        """Switch to a specific outdoor station.

        The device command cycles rather than selects, so this repeats until
        the reported station matches.
        """
        session = await self._async_with_video()
        if self.station == target:
            # The command cycles rather than selects, so acting here would
            # visit the other station and come back - a visible flicker for
            # what the user asked to be a no-op.
            return True
        async with self._command_lock:
            for _ in range(MAX_CYCLE_ATTEMPTS):
                current = await session.async_cycle_unit()
                self.station = current
                if current == target:
                    self._notify()
                    return True
        _LOGGER.warning(
            "Could not reach outdoor station %s after %s attempts (now on %s)",
            target,
            MAX_CYCLE_ATTEMPTS,
            self.station,
        )
        self._notify()
        return False

    async def async_open_lock(self) -> None:
        """Release the door lock on the active station."""
        session = await self._async_with_video()
        async with self._command_lock:
            await session.async_trigger_key()

    async def async_open_gate(self) -> None:
        """Release the gate on the active station."""
        session = await self._async_with_video()
        async with self._command_lock:
            await session.async_trigger_gate()

    async def async_set_quality(self, quality: str) -> None:
        self.quality = quality
        session = self.session
        if session is not None and session.connected and self._video_running:
            async with self._command_lock:
                await session.async_set_quality(quality)
        self._notify()

    async def async_restart_video(self) -> None:
        """Stop the device stream and start it again.

        The picture going black is normal for this device - the stream simply
        stops and there is no error anywhere - and there is otherwise no way
        to ask for it back without waiting out the idle timeout. Re-picking
        the station in the dropdown does not do it either: Home Assistant does
        not call a select entity when the value has not changed.

        The station is re-established afterwards rather than assumed, since a
        fresh start_video may not land where the last one left off.
        """
        if self._restarting:
            # A second click while the first is still going. Queuing it would
            # tear down the stream that is being brought up and do the whole
            # thing again, so the button stays responsive by ignoring it.
            _LOGGER.debug("Restart already in progress, ignoring this one")
            return
        wanted = self.station
        # Held across both halves: a stream_source() landing in the gap would
        # otherwise start the video itself and one of the two starts would come
        # back "video busy".
        self._restarting = True
        try:
            await self._async_restart_video_inner(wanted)
        finally:
            self._restarting = False

    async def _async_restart_video_inner(self, wanted: int | None) -> None:
        async with self._video_lock:
            await self._async_stop_video_locked()
            # What we knew about the station does not hold across a restart.
            self.station = None
            await self._async_start_video_locked()
        if wanted is not None:
            with contextlib.suppress(UrmetError):
                await self.async_select_station(wanted)
        _LOGGER.debug("Video restarted (station %s)", self.station)
        self._notify()

    async def async_answer(self) -> None:
        """Take the call in Home Assistant: hold video and open the mic path.

        The device never pushes media on a ring - a client has to ask - so
        nothing is grabbed until someone deliberately answers here. That is the
        whole reason video is not held speculatively: the channel is single
        occupancy, and while we have it the phone app cannot answer.
        """
        await self._async_start_video()
        await self.async_talk_start()
        self._mark_activity()
        _LOGGER.debug("Call answered in Home Assistant")

    async def async_hang_up(self) -> None:
        """Release the call, and the channel, immediately.

        Deliberately does not wait for the idle timer: the point of hanging up
        is to hand the video channel back so the phone app can take over.
        """
        with contextlib.suppress(UrmetError):
            await self.async_talk_stop()
        with contextlib.suppress(UrmetError):
            await self._async_stop_video()
        _LOGGER.debug("Call released")

    async def async_talk_start(self) -> None:
        session = await self._async_with_video()
        async with self._command_lock:
            await session.async_talk_start()
        self.talking = True
        self._notify()

    async def async_talk_stop(self) -> None:
        session = self.session
        if session is not None and session.connected:
            async with self._command_lock:
                await session.async_talk_stop()
        self.talking = False
        self._notify()

    async def async_play_audio(self, source: str, ffmpeg_binary: str) -> None:
        """Speak a media source or TTS clip at the door station."""
        pcmu = await urmet_audio.async_transcode_to_mulaw(source, ffmpeg=ffmpeg_binary)
        session = await self._async_with_video()
        already_talking = self.talking
        if not already_talking:
            await self.async_talk_start()
        try:
            await urmet_audio.async_send_audio(session, pcmu, repeat=self.talk_repeat)
            # Let the tail drain before closing, or the last frames are cut off.
            await asyncio.sleep(0.5)
        finally:
            if not already_talking:
                with contextlib.suppress(UrmetError):
                    await self.async_talk_stop()

    # -- state --------------------------------------------------------------

    @property
    def device_info_raw(self) -> dict:
        return self.session.device_info if self.session else {}

    @property
    def streaming(self) -> bool:
        return self._video_running

    @property
    def viewers(self) -> int:
        return self.pipeline.client_count

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, f"{SIGNAL_STATE_UPDATED}_{self.entry_id}")
