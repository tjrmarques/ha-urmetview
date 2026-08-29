#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Sit on a logged-in session and log everything the device sends unprompted.

This is capture 3: it answers whether the doorbell ring reaches a client that is
logged in but is *not* streaming video.

Deliberately does NOT start video, for two reasons: it leaves the video channel
free so the phone app still works, and it keeps the traffic to a trickle so a
router-side packet capture will not hit its size limit while you walk to the
door and back.

    uv run tools/urmet_listen.py --auth <hash> --host 10.0.50.6 --port 20043

Then ring the bell and watch. Anything that appears in the "UNSOLICITED" section
is a message the device sent on its own - which is what a ring would look like.

Add --with-video to repeat the test with the stream running, if the idle test
comes up empty.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time

from _common import (
    UrmetSession,
    add_common_args,
    async_resolve,
    protocol,
    setup_logging,
)


class Listener:
    def __init__(self, quiet: bool) -> None:
        self.start = time.monotonic()
        self.quiet = quiet
        self.unsolicited: list[tuple[float, int, str]] = []
        self.raw_types: dict[int, int] = {}
        self.known_subcmds = {
            protocol.SUBCMD_HELLO,  # device announces itself all session long
        }

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def on_raw(self, data: bytes) -> None:
        if len(data) >= 2 and data[0] == protocol.MAGIC:
            self.raw_types[data[1]] = self.raw_types.get(data[1], 0) + 1

    def on_command(self, response: protocol.CommandResponse) -> None:
        if response.subcmd in self.known_subcmds and self.quiet:
            return
        marker = " "
        if response.subcmd not in self.known_subcmds:
            marker = "*"
            self.unsolicited.append((self.elapsed(), response.subcmd, response.text))
        print(
            f"{self.elapsed():8.3f} {marker} subcmd=0x{response.subcmd:04x} "
            f"seq={response.seq:<5} {response.text[:160]}",
            flush=True,
        )


async def _run(args: argparse.Namespace) -> int:
    host, port = await async_resolve(args)
    listener = Listener(quiet=not args.show_all)

    session = UrmetSession(host, port, args.uid, args.auth, args.username)
    session.on_command = listener.on_command
    session.on_raw = listener.on_raw

    print(f"Connecting to {host}:{port} ...")
    await session.async_connect()
    print("Logged in. Device reports:", session.device_info or "(nothing yet)")

    if args.with_video:
        print("Starting video as requested (--with-video)...")
        await session.async_start_video(args.quality)
        print("Video started.")
    else:
        print("NOT starting video - the video channel stays free for the phone app.")

    print()
    print("=" * 70)
    print("LISTENING. Go ring the doorbell, then come back. Ctrl-C to stop.")
    print("Lines marked * are messages the device sent unprompted.")
    print("=" * 70)
    print()

    try:
        while True:
            await asyncio.sleep(args.status_interval)
            print(
                f"{listener.elapsed():8.3f}   [still alive] "
                f"video_frames={session.video_frames} audio_frames={session.audio_frames} "
                f"unsolicited={len(listener.unsolicited)}",
                flush=True,
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\nClosing session cleanly...")
        with contextlib.suppress(Exception):
            await session.async_close()

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  ran for {listener.elapsed():.1f}s")
    print("  inbound packet types:")
    for msg_type, count in sorted(listener.raw_types.items()):
        print(f"    0x{msg_type:02x}: {count}")
    print(f"  UNSOLICITED command blocks: {len(listener.unsolicited)}")
    for when, subcmd, text in listener.unsolicited:
        print(f"    {when:8.3f} subcmd=0x{subcmd:04x} {text[:200]}")
    if not listener.unsolicited:
        print("\n  Nothing unsolicited arrived. If you rang the bell during this run,")
        print("  the ring does not reach an idle logged-in session, and the doorbell")
        print("  needs a different source (see the plan's doorbell section).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser)
    parser.add_argument(
        "--with-video", action="store_true", help="also start the video stream"
    )
    parser.add_argument("--quality", default="sd", choices=["ld", "sd", "hd"])
    parser.add_argument("--status-interval", type=float, default=10.0)
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="also log the device's routine announcements",
    )
    args = parser.parse_args()
    setup_logging(args.verbose)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
