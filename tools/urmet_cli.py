#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""General-purpose CLI: video, station selection, lock and gate.

    # watch live (then: ffplay -f h264 -i tcp://127.0.0.1:5599 -fflags nobuffer)
    uv run tools/urmet_cli.py --auth <hash> video --serve

    # record 10s of raw H.264
    uv run tools/urmet_cli.py --auth <hash> video --out clip.h264 --duration 10

    # switch to outdoor station 2, then release the lock there
    uv run tools/urmet_cli.py --auth <hash> station 2
    uv run tools/urmet_cli.py --auth <hash> lock

Video is started before any command because the device answers 'busy' to
station/lock/gate otherwise. The session is always torn down on exit - skipping
that leaves the video channel held until the device times it out, and the next
connect is refused with 'video busy'.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from _common import UrmetSession, add_common_args, async_resolve, setup_logging

MAX_CYCLE_ATTEMPTS = 4


async def async_select_station(session: UrmetSession, target: int) -> bool:
    """Select a specific outdoor station.

    The device's command *cycles* rather than selecting - it takes no parameter
    and reports which station it landed on - so this calls it until the reported
    station matches, bounded so a device that reports something unexpected
    cannot spin forever.
    """
    for _ in range(MAX_CYCLE_ATTEMPTS):
        current = await session.async_cycle_unit()
        print(f"  station is now {current}")
        if current == target:
            return True
    return False


class VideoSink:
    """Reassembles the H.264 elementary stream and fans it out."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.clients: list[asyncio.StreamWriter] = []
        self.pending: list[asyncio.StreamWriter] = []
        self.frames = 0
        self.keyframes = 0
        self.last_keyframe: bytes | None = None
        self._current = bytearray()
        self._current_is_key = False

    def feed(self, data: bytes, frame_start: bool, keyframe: bool) -> None:
        if frame_start:
            if self._current:
                completed = bytes(self._current)
                if self._current_is_key:
                    self.last_keyframe = completed
                    self.keyframes += 1
                # A new client can only be joined here, at a frame boundary.
                # Splicing it in mid-frame appends a partial NAL to whatever we
                # already sent and decodes as "Failed to parse header of NALU".
                if self.pending:
                    self.clients.extend(self.pending)
                    self.pending.clear()
            self._current = bytearray(data)
            self._current_is_key = keyframe
            self.frames += 1
        else:
            self._current += data

        self.buffer += data
        dead = []
        for client in self.clients:
            try:
                client.write(data)
            except (OSError, ConnectionError):
                dead.append(client)
        for client in dead:
            self.clients.remove(client)

    def add_client(self, writer: asyncio.StreamWriter) -> None:
        # Prime with the last keyframe so the decoder has an SPS/PPS/IDR to
        # reference; a P-frame alone decodes to nothing.
        if self.last_keyframe is not None:
            with contextlib.suppress(OSError, ConnectionError):
                writer.write(self.last_keyframe)
        self.pending.append(writer)


async def _cmd_video(session: UrmetSession, args: argparse.Namespace) -> int:
    sink = VideoSink()
    session.on_video = sink.feed

    await session.async_start_video(args.quality)
    print(f"Video started (quality={args.quality}).")

    server = None
    if args.serve:

        async def on_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            print("  ffplay connected")
            sink.add_client(writer)
            with contextlib.suppress(Exception):
                await reader.read()

        server = await asyncio.start_server(on_client, "127.0.0.1", args.tcp_port)
        print(
            f"\n  ffplay -f h264 -i tcp://127.0.0.1:{args.tcp_port} "
            f"-fflags nobuffer -flags low_delay -framedrop\n"
        )

    if args.station:
        await async_select_station(session, args.station)

    deadline = (
        asyncio.get_running_loop().time() + args.duration if args.duration else None
    )
    try:
        while deadline is None or asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2.0)
            print(
                f"  frames={sink.frames} keyframes={sink.keyframes} "
                f"bytes={len(sink.buffer)} audio={session.audio_frames}",
                flush=True,
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if server is not None:
            server.close()

    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(bytes(sink.buffer))
        print(f"\nWrote {args.out} ({len(sink.buffer)} bytes)")
        print(f"Convert with: ffmpeg -f h264 -i {args.out} -c:v copy out.mp4")
    if sink.keyframes == 0 and sink.frames:
        print(
            "\nWARNING: no keyframes arrived, only P-frames - this data cannot be "
            "decoded. That normally means acks are not reaching the device.",
            file=sys.stderr,
        )
    return 0


async def _run(args: argparse.Namespace) -> int:
    host, port = await async_resolve(args)
    session = UrmetSession(host, port, args.uid, args.auth, args.username)
    print(f"Connecting to {host}:{port} ...")
    await session.async_connect()
    print("Logged in:", session.device_info or "(no device info yet)")

    try:
        if args.command == "info":
            await session.async_query_device_info()
            await asyncio.sleep(1.0)
            print("Device info:", session.device_info)
            return 0

        if args.command == "video":
            return await _cmd_video(session, args)

        # station/lock/gate all need a live video session first.
        await session.async_start_video("sd")
        print("Video session started (required before commands).")

        if args.command == "station":
            ok = await async_select_station(session, args.which)
            print("Selected." if ok else f"Could not reach station {args.which}")
            return 0 if ok else 1
        if args.command == "lock":
            await session.async_trigger_key()
            print("Door lock released.")
        elif args.command == "gate":
            await session.async_trigger_gate()
            print("Gate released.")
        return 0
    finally:
        print("Closing session...")
        with contextlib.suppress(Exception):
            await session.async_close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    video = sub.add_parser("video", help="start the stream, record and/or serve it")
    video.add_argument("--quality", default="sd", choices=["ld", "sd", "hd"])
    video.add_argument("--duration", type=float, default=0.0, help="0 = until Ctrl-C")
    video.add_argument("--out", help="write raw H.264 here")
    video.add_argument(
        "--serve", action="store_true", help="serve H.264 on a local TCP port"
    )
    video.add_argument("--tcp-port", type=int, default=5599)
    video.add_argument(
        "--station", type=int, choices=[1, 2], help="switch station first"
    )

    station = sub.add_parser("station", help="select an outdoor station")
    station.add_argument("which", type=int, choices=[1, 2])

    sub.add_parser("lock", help="release the door lock on the active station")
    sub.add_parser("gate", help="release the gate on the active station")
    sub.add_parser("info", help="query device info and exit")

    args = parser.parse_args()
    setup_logging(args.verbose)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
