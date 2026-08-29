"""Outbound talk audio: transcoding and paced delivery.

The device wants G.711 mu-law, 8 kHz, mono, in 320-byte (40 ms) frames. ffmpeg
does the transcode, which avoids depending on ``audioop`` - removed from the
stdlib in Python 3.13, which Home Assistant already runs on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

from .const import AUDIO_FRAME_BYTES, AUDIO_SAMPLE_RATE, DEFAULT_TALK_REPEAT

_LOGGER = logging.getLogger(__name__)

#: Wall-clock duration of one frame. Talk audio must be paced at roughly real
#: time: dumping a whole file at once overruns the device's jitter buffer and
#: comes out as a garbled burst.
FRAME_INTERVAL = AUDIO_FRAME_BYTES / AUDIO_SAMPLE_RATE  # 0.04s


class TranscodeError(Exception):
    """ffmpeg could not decode the requested audio."""


async def async_transcode_to_mulaw(source: str, ffmpeg: str = "ffmpeg") -> bytes:
    """Decode any ffmpeg-readable source to raw 8 kHz mono mu-law."""
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source,
        "-f",
        "mulaw",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-ac",
        "1",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise TranscodeError(
            f"ffmpeg failed on {source!r}: {stderr.decode('utf-8', 'replace').strip()}"
        )
    if not stdout:
        raise TranscodeError(f"ffmpeg produced no audio for {source!r}")
    return stdout


def iter_frames(pcmu: bytes) -> Iterator[bytes]:
    """Split mu-law bytes into exact 320-byte frames.

    A short final frame is padded with 0xFF (mu-law silence) rather than sent
    undersized, since the device's frame header declares a fixed length.
    """
    for offset in range(0, len(pcmu), AUDIO_FRAME_BYTES):
        frame = pcmu[offset : offset + AUDIO_FRAME_BYTES]
        if len(frame) < AUDIO_FRAME_BYTES:
            frame = frame.ljust(AUDIO_FRAME_BYTES, b"\xff")
        yield frame


async def async_send_audio(
    session,
    pcmu: bytes,
    repeat: int = DEFAULT_TALK_REPEAT,
) -> int:
    """Stream mu-law audio to the device in real time. Returns frames sent.

    Pacing uses a monotonic deadline per frame rather than ``sleep(0.04)`` so
    the send rate does not drift with the time each send actually takes.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time()
    count = 0
    for frame in iter_frames(pcmu):
        session.send_talk_frame(frame, repeat=repeat)
        count += 1
        deadline += FRAME_INTERVAL
        delay = deadline - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        elif delay < -1.0:
            # We have fallen more than a second behind; resync rather than
            # trying to catch up with a burst.
            _LOGGER.debug("Talk pacing fell %.2fs behind, resyncing", -delay)
            deadline = loop.time()
    return count
