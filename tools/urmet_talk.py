#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Push-to-talk spike: send audio out of the door station's speaker.

This is the first half of the PTT feature, deliberately built before any UI. If
mu-law frames do not come out of the speaker from a plain CLI, no amount of
Lovelace card work will help - so prove the audio path here first.

    # say something (any ffmpeg-readable file or URL)
    uv run tools/urmet_talk.py --auth <hash> --file hello.wav

    # or a test tone, if you just want to hear *anything*
    uv run tools/urmet_talk.py --auth <hash> --tone

Requires ffmpeg on PATH for the transcode to 8 kHz mono mu-law.

Video is started first because the device refuses unit/key/gate - and, we
assume, talk - unless a video session is live. Use --no-video to test whether
talk actually needs it; that is worth knowing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import struct
import tempfile
import wave
from pathlib import Path

from _common import UrmetSession, add_common_args, async_resolve, setup_logging
from urmet import audio


def _make_tone(path: Path, seconds: float = 2.0, freq: float = 440.0) -> None:
    """Write a plain sine wave, so there is always something to send."""
    rate = 8000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(int(rate * seconds)):
            value = int(16000 * math.sin(2 * math.pi * freq * index / rate))
            frames += struct.pack("<h", value)
        handle.writeframes(bytes(frames))


async def _run(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        source = args.file
        if args.tone:
            tone_path = Path(tmp) / "tone.wav"
            _make_tone(tone_path, seconds=args.duration)
            source = str(tone_path)
        if not source:
            print("Give me --file <audio> or --tone")
            return 2

        print(f"Transcoding {source} to 8kHz mono mu-law...")
        pcmu = await audio.async_transcode_to_mulaw(source, ffmpeg=args.ffmpeg)
        seconds = len(pcmu) / 8000
        print(f"  {len(pcmu)} bytes = {seconds:.1f}s of audio")

        host, port = await async_resolve(args)
        session = UrmetSession(host, port, args.uid, args.auth, args.username)
        print(f"Connecting to {host}:{port} ...")
        await session.async_connect()
        print("Logged in.")

        try:
            if not args.no_video:
                print("Starting video (commands are refused as 'busy' without it)...")
                await session.async_start_video(args.quality)

            print("Opening the talk channel...")
            await session.async_talk_start()

            print(f"Sending {seconds:.1f}s of audio (repeat={args.repeat})... listen at the door.")
            sent = await audio.async_send_audio(session, pcmu, repeat=args.repeat)
            print(f"Sent {sent} frames.")

            # Let the tail drain before tearing the channel down, or the last
            # few frames get cut off.
            await asyncio.sleep(0.5)
        finally:
            print("Stopping talk and closing session...")
            with contextlib.suppress(Exception):
                await session.async_talk_stop()
            with contextlib.suppress(Exception):
                await session.async_close()

    print("\nDid you hear it? If not, try --repeat 12 (what the app sends) and -vv.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser)
    parser.add_argument("--file", help="any ffmpeg-readable audio file or URL")
    parser.add_argument("--tone", action="store_true", help="send a generated sine tone instead")
    parser.add_argument("--duration", type=float, default=2.0, help="tone length in seconds")
    parser.add_argument("--repeat", type=int, default=6, help="retransmits per frame (app uses ~12)")
    parser.add_argument("--quality", default="sd", choices=["ld", "sd", "hd"])
    parser.add_argument("--no-video", action="store_true", help="skip start_video first")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    setup_logging(args.verbose)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
