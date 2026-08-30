#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Serve exactly what Home Assistant is given: one muxed MPEG-TS port.

Every other tool here stops short of that. urmet_cli serves raw H.264, and
handing video and audio to ffmpeg on two separate ports tests a pipeline we do
not ship. Home Assistant gets a single TCP port carrying muxed MPEG-TS, go2rtc
reads that, and if it is wrong nothing downstream can be right.

This runs the integration's own MediaPipeline - imported, not reimplemented -
so the relay fan-out and the PAT-admission logic are the code under test
rather than a copy of it.

    uv run tools/urmet_stream.py --auth <hash>
    # then, in another terminal, at the port it prints:
    ffplay tcp://127.0.0.1:<port>

    # capture the same bytes and inspect the program:
    uv run tools/urmet_stream.py --auth <hash> --dump out.ts
    ffprobe out.ts

The thing to check first is whether the program contains audio at all. A live
capture showed go2rtc seeing only 'video, recvonly, H264' from us, which makes
Home Assistant add its own #audio=opus producer - and that producer is what
breaks the WebRTC offer.

    --no-audio   video only, so the mux has one input like the working viewer
    --aspect     picture aspect correction, "" to disable
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import shutil
import sys
import time

from _common import UrmetSession, add_common_args, async_resolve, load_media_module
from _common import setup_logging

media = load_media_module()


class Recorder:
    """Consume the relay like any other client, and tee it to a file.

    Deliberately a normal TCP consumer rather than a tap inside the pipeline:
    what lands in the file is then exactly what go2rtc would have received,
    admission logic included.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.bytes = 0
        self._task: asyncio.Task | None = None

    async def _run(self, port: int) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            with open(self.path, "wb") as handle:
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    handle.write(chunk)
                    self.bytes += len(chunk)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def start(self, port: int) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(port))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


async def _run(args: argparse.Namespace) -> int:
    ffmpeg = shutil.which(args.ffmpeg)
    if ffmpeg is None:
        print(f"ffmpeg not found as {args.ffmpeg!r}", file=sys.stderr)
        return 2

    host, port = await async_resolve(args)
    session = UrmetSession(host, port, args.uid, args.auth, args.username)
    pipeline = media.MediaPipeline(
        ffmpeg, enable_audio=not args.no_audio, pixel_aspect=args.aspect
    )
    session.on_video = pipeline.feed_video
    session.on_audio = pipeline.feed_audio

    recorder = Recorder(args.dump) if args.dump else None
    try:
        await session.async_connect()
        print(f"Logged in to {host}:{port}")
        await pipeline.async_start()
        await session.async_start_video(args.quality)
        if not args.no_audio:
            with contextlib.suppress(Exception):
                await session.async_start_audio()

        print()
        print("=" * 70)
        print(f"  MPEG-TS on tcp://127.0.0.1:{pipeline.out_port}")
        print("=" * 70)
        print("  This is byte-for-byte what Home Assistant hands go2rtc.")
        print()
        print(f"    ffplay tcp://127.0.0.1:{pipeline.out_port}")
        print(f"    ffprobe tcp://127.0.0.1:{pipeline.out_port}")
        if recorder is not None:
            print(f"\n  ...and teeing to {args.dump}; ffprobe it afterwards to see")
            print("  whether the program actually carries audio.")
        print()

        if recorder is not None:
            recorder.start(pipeline.out_port)

        started = time.time()
        last = 0.0
        while args.duration <= 0 or time.time() - started < args.duration:
            await asyncio.sleep(1.0)
            now = time.time() - started
            if now - last >= 3.0:
                last = now
                extra = f" | dumped {recorder.bytes} B" if recorder else ""
                print(
                    f"[{now:5.0f}s] frames={pipeline.frames} "
                    f"keyframes={pipeline.keyframes} "
                    f"consumers={pipeline.client_count}{extra}",
                    file=sys.stderr,
                    flush=True,
                )
    except KeyboardInterrupt:
        pass
    finally:
        if recorder is not None:
            await recorder.stop()
        # Teardown matters: without it the device holds the video channel and
        # the next connect is refused with "video busy".
        with contextlib.suppress(Exception):
            await pipeline.async_shutdown()
        with contextlib.suppress(Exception):
            await session.async_close()
        print("\nSession closed.")
    if recorder is not None:
        print(f"Wrote {recorder.bytes} bytes to {args.dump}")
        print(f"  ffprobe {args.dump}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser)
    parser.add_argument("--quality", default="sd", choices=["ld", "sd", "hd"])
    parser.add_argument(
        "--no-audio", action="store_true", help="video only - a single-input mux"
    )
    parser.add_argument(
        "--aspect",
        default="1/2",
        help='picture aspect correction; "" to send the bitstream untouched',
    )
    parser.add_argument("--dump", help="also write the served MPEG-TS to this file")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument(
        "--duration", type=float, default=0.0, help="seconds; 0 = until Ctrl-C"
    )
    args = parser.parse_args()
    setup_logging(args.verbose)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
